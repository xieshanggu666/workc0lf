# -*- coding: utf-8 -*-
"""末日地堡生存核心引擎。

资源守恒循环：
  每日净变化 = 设施产出 - 人口消耗 - 运营损耗
  产出受设施等级 + 人力资源(工程师/农夫加成) + 士气系数影响
"""

from sqlalchemy.orm import Session

from ..models import GameSession, Resident, Facility, EventLog
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY

import uuid

# 资源键
FOOD, WATER, POWER, OXY = "food", "water", "power", "oxygen"
RESOURCE_KEYS = [FOOD, WATER, POWER, OXY]

# 每日人均基础消耗
BASE_CONSUME = {FOOD: 1.5, WATER: 1.3, POWER: 1.0, OXY: 0.8}

# 设施基础产出（等级1）
FACILITY_OUTPUT = {
    "farm": {FOOD: 6.0, POWER: -1.5},   # 菜园产食物，耗电
    "water": {WATER: 7.0, POWER: -1.0}, # 净水器产水，耗电
    "power": {POWER: 8.0},              # 发电机产电
    "oxygen": {OXY: 6.0, POWER: -1.0},  # 水培/制氧耗电产氧
    "med": {},                          # 医疗：加速回复健康，微耗电
    "storage": {},                      # 仓库：降低损耗
}
FACILITY_LEVEL_SCALE = 1.6  # 升级产出按比例放大
FACILITY_COST = {  # 建造/升级消耗 builder cost
    1: {FOOD: 20, WATER: 10, POWER: 15},
    2: {FOOD: 35, WATER: 18, POWER: 28},
    3: {FOOD: 60, WATER: 30, POWER: 45},
}

# 岗位
JOB_EFFICIENCY = {"engineer": 1.25, "farmer": 1.3, "general": 1.0}

# 危机事件概率
CRISIS_DAY_CHANCE = 0.45

# 外出探索：每名队员出发至少携带的口粮/饮水；物资在出发时离堡，返程不返还
EXPEDITION_PROVISION_PER_PERSON = {FOOD: 2.0, WATER: 1.0}
EXPEDITION_MAX_DEPTH = 3


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _rng():
    """简单投影式随机数，便于测试时可注入 seed。"""
    import random
    return random.Random()


class BunkerEngineError(Exception):
    pass


class BunkerEngineConflict(BunkerEngineError):
    """并发冲突（乐观锁版本不匹配），HTTP 层映射为 409。"""


# 档案状态机阶段：
#   daily      —— 每日阶段，可建造/升级/调岗，可推进一天或派出探索队
#   crisis     —— 危机阶段，存在待处理危机，除结算危机外拒绝一切推进与经营动作
#   expedition —— 探索遭遇阶段，探索队外出且等待玩家处理遭遇/选择是否返程
#   ended      —— 终局（win/over），拒绝任何状态变更
PHASE_DAILY, PHASE_CRISIS, PHASE_EXPEDITION, PHASE_ENDED = "daily", "crisis", "expedition", "ended"


class BunkerEngine:
    def __init__(self, db: Session, session: GameSession, rand=None):
        self.db = db
        self.session = session
        self.rand = rand or _rng()

    # ---- 状态机 ----
    @property
    def phase(self):
        if self.session.status != "running":
            return PHASE_ENDED
        if self.session.pending_crisis:
            return PHASE_CRISIS
        if self.active_expedition() and self.active_expedition().get("pending_encounter"):
            return PHASE_EXPEDITION
        return PHASE_DAILY

    def _require_phase(self, phase, message):
        if self.phase != phase:
            raise BunkerEngineError(message)

    # ---- 资源查询 ----
    def get_resources(self):
        return self.session.resources or {k: 0 for k in RESOURCE_KEYS}

    def _set_resource(self, key, val):
        # 复制后整体回写，确保 JSON 列的变更被 SQLAlchemy 追踪并落库
        res = dict(self.session.resources or {k: 0 for k in RESOURCE_KEYS})
        res[key] = round(max(0.0, val), 1)
        self.session.resources = res

    def _add_resource(self, key, delta):
        res = self.session.resources or {k: 0 for k in RESOURCE_KEYS}
        cur = res.get(key, 0.0)
        nxt = max(0.0, cur + delta)
        new_res = dict(res)
        new_res[key] = round(nxt, 1)
        self.session.resources = new_res
        return nxt

    def active_expedition(self):
        return getattr(self.session, "active_expedition", None)

    def away_resident_ids(self):
        expedition = self.active_expedition()
        if not expedition:
            return set()
        return {p["id"] for p in expedition.get("party", [])}

    def home_residents(self, alive_only=True):
        away = self.away_resident_ids()
        return [
            r for r in self.session.residents
            if r.id not in away and (r.alive if alive_only else True)
        ]

    # ---- 设施 ----
    def facility_output(self, facility: Facility):
        base = FACILITY_OUTPUT.get(facility.category, {})
        mult = FACILITY_LEVEL_SCALE ** (facility.level - 1)
        out = {k: v * mult for k, v in base.items()}
        # 农夫/工程师提升产出设施
        if facility.category in ("farm", "oxygen") and self.job_count("farmer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("farmer")
        if facility.category == "power" and self.job_count("engineer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("engineer")
        return out

    def job_count(self, job):
        # 外出探索者暂停地堡生产，不再给设施提供岗位加成
        return sum(1 for r in self.home_residents() if r.job == job)

    def active_facilities(self):
        return [f for f in self.session.facilities if f.status == "active"]

    # ---- 每日推进 ----
    def advance_day(self):
        # 待处理危机/探索遭遇都必须先处理；终局拒绝推进，返程中的探索队允许推进归队
        self._ensure_running()
        if self.session.pending_crisis or (self.active_expedition() and self.active_expedition().get("pending_encounter")):
            raise BunkerEngineError("存在待处理事件，必须先做出抉择才能推进")
        self.session.day += 1
        # 先按“离堡人口仍未归队”的状态完成今日地堡循环；探索队随后结算，
        # 避免获救者在加入当天被额外计一次堡内消耗，也保证伤亡/战利品只写一次
        if self.active_expedition() or self.session.survivors > 0:
            self._apply_production_and_consumption()
            self._apply_health_morale()
        # 堡内已在日常循环中人口归零且无人在外时立即终局；探索队尚在时等待归队结算
        if self.session.survivors <= 0 and not self.active_expedition() and self._check_end():
            return None
        expedition = self.active_expedition()
        if expedition and (expedition.get("returning") or self.session.day >= self.session.target_day):
            self._settle_expedition()
        # 终局优先：抵达目标日或全面崩溃直接结算结局，不再凭空挂起一个
        # 永远无法处理的危机（统一每日推进 → 危机/探索处理 → 终局的流转）
        if self._check_end():
            return None
        if self.active_expedition():
            return self._begin_expedition_encounter()
        return self._maybe_trigger_crisis()

    def _apply_production_and_consumption(self):
        # 离堡探索者暂停地堡生产，也不在堡内消耗当日口粮/水/电/氧；
        # 他们携带的补给已在出发时统一离库，返程后再并入地堡循环
        pop = len(self.home_residents())
        # 士气系数(平均士气)：低士气降低产出
        avg_morale = self.avg_morale()
        morale_factor = 0.6 + 0.4 * (avg_morale / 100.0)

        # 消耗
        consume = {}
        for k in RESOURCE_KEYS:
            consume[k] = BASE_CONSUME[k] * pop

        # 产出（累计设施净产）
        prod = {k: 0.0 for k in RESOURCE_KEYS}
        for f in self.active_facilities():
            for k, v in self.facility_output(f).items():
                prod[k] += v * morale_factor

        # 应用净变化（消耗优先，产出后）
        for k in RESOURCE_KEYS:
            net = prod.get(k, 0.0) - consume[k]
            self._add_resource(k, net)

        # 日志
        self._log(
            "update",
            f"第{self.session.day}天 · 生存更新",
            f"堡内人口{pop}，食物净变{round(consume[FOOD]-prod[FOOD],1):+}、水{round(consume[WATER]-prod[WATER],1):+}、电力{round(consume[POWER]-prod[POWER],1):+}、氧气{round(consume[OXY]-prod[OXY],1):+}",
            decision="例行更新",
        )

    def _apply_health_morale(self):
        res = self.get_resources()
        # 资源见底，健康/士气下降；探索途中的伤亡与士气在归队时统一结算
        for r in self.home_residents():
            morale = r.morale
            # 资源不足影响
            for k, name in ((FOOD, "食物"), (WATER, "水源"), (OXY, "氧气"), (POWER, "电力")):
                if res.get(k, 0) <= 15:
                    morale -= 2.0
            # 医疗站回复 + 保持士气
            if self.has_category("med"):
                if r.health < 100:
                    r.health = _clamp(r.health + 1.2)
            # 低健康拖累士气
            if r.health < 30:
                morale -= 3.0
            # 士气自然衰减/恢复向基准 75
            if morale < 75:
                morale += 0.5
            elif morale > 80:
                morale -= 0.3
            r.morale = _clamp(morale)
        # 去除最严重短缺导致的死亡
        self._apply_starvation_deaths()

    def has_category(self, cat):
        return any(f.category == cat and f.status == "active" for f in self.session.facilities)

    def _apply_starvation_deaths(self):
        res = self.get_resources()
        critical = [k for k in RESOURCE_KEYS if res.get(k, 0) <= 0]
        if not critical:
            return
        # 每日最多因匮乏死 1 人，依次从堡内最弱居民开始；探索途中另有伤亡结算
        alive = self.home_residents()
        if not alive:
            return
        weakest = min(alive, key=lambda r: r.health)
        weakest.alive = 0
        weakest.health = 0
        self.session.survivors -= 1
        self._log("crisis", "生存危机：资源耗尽", f"{weakest.name} 因匮乏失去生命。", decision="自然事件")

    def avg_morale(self):
        alive = self.home_residents() if self.active_expedition() else [r for r in self.session.residents if r.alive]
        if not alive:
            return 0.0
        return sum(r.morale for r in alive) / len(alive)

    def _log(self, etype, title, detail, decision=None):
        self.db.add(
            EventLog(
                session_id=self.session.id,
                day=self.session.day,
                event_type=etype,
                title=title,
                detail=detail,
                decision=decision,
            )
        )

    # ---- 危机轮盘 ----

    @staticmethod
    def _effect_scope(effect):
        """健康/士气效果的作用域：'single' 仅目标本人，'all' 全体存活者。

        数字简写默认为全体；单体效果须显式声明
        {"value": -5, "target": "single"}。
        """
        if isinstance(effect, dict):
            return effect.get("target", "all")
        return "all"

    @staticmethod
    def _effect_value(effect):
        return effect["value"] if isinstance(effect, dict) else effect

    def _event_needs_target(self, event):
        """事件是否存在只作用于单个居民的决策；只有这类事件才随机目标。"""
        for c in event["choices"]:
            effects = c.get("effects", {})
            for stat in ("health", "morale"):
                if stat in effects and self._effect_scope(effects[stat]) == "single":
                    return True
        return False

    def _maybe_trigger_crisis(self):
        if self.rand.random() > CRISIS_DAY_CHANCE:
            return None
        event = self.rand.choice(CRISIS_POOL)
        crisis = self._build_crisis(event)
        # 待处理危机整体写入存档：事件、目标、选项与一次性 token 一起绑定，
        # 刷新页面后凭档案即可恢复同一个决策
        self.session.pending_crisis = crisis
        return crisis

    def _build_crisis(self, event):
        # 仅当事件存在单体效果的决策时才抽取受影响居民；
        # 全体事件不产生目标，前端也无从回传 target_id
        needs_target = self._event_needs_target(event)
        # 危机发生在地堡：外出探索者暂停生产，不作为地堡危机的随机目标
        alive = self.home_residents()
        target = self.rand.choice(alive) if needs_target and alive else None
        return {
            "token": uuid.uuid4().hex,  # 本次待处理危机的一次性凭据
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    @classmethod
    def _choice_targeted(cls, choice):
        """该决策是否含只作用于目标本人的健康/士气效果。"""
        effects = choice.get("effects", {})
        return any(
            cls._effect_scope(effects[stat]) == "single"
            for stat in ("health", "morale")
            if stat in effects
        )

    def _ensure_running(self):
        """结算边界：游戏结束后拒绝一切状态变更。"""
        if self.session.status != "running":
            raise BunkerEngineError("游戏已结束，无法执行该操作")

    def _require_daily_phase(self, action):
        """经营/推进/出发类动作只允许在每日阶段执行。"""
        self._ensure_running()
        if self.phase == PHASE_CRISIS:
            raise BunkerEngineError(f"存在待处理危机，必须先完成抉择才能{action}")
        if self.phase == PHASE_EXPEDITION:
            raise BunkerEngineError(f"探索队正在等待遭遇抉择，必须先处理或安排返程才能{action}")

    def _pending_event(self):
        """取出当前待处理危机对应的事件定义；存档损坏时视为无法结算。"""
        pending = self.session.pending_crisis
        if not pending:
            return None, None
        event_key = pending.get("event")
        event = next((e for e in CRISIS_POOL if e["key"] == event_key), None)
        if event is None:
            raise BunkerEngineError("待处理危机已失效，请刷新档案后重试")
        return pending, event

    @staticmethod
    def _matches_resolution(rec, event_key, choice_key, target_id, day=None):
        """判断落败/重试请求是否就是上一次已完成的那次结算（幂等回放）。

        除事件/选项/目标外还核对危机发生日，避免不同天的同类型危机被误重放；
        day 为 None（调用方拿不到上下文）时退化为不校验天数。
        """
        if not rec or rec.get("event") != event_key or rec.get("choice") != choice_key:
            return False
        if day is not None and rec.get("day") is not None and rec.get("day") != day:
            return False
        return (rec.get("target_id") or None) == (target_id or None)

    def _resolve_target(self, target_id, required):
        """统一解析目标居民。

        - required=True（所选决策含单体效果）：必须显式给出目标，且目标归属
          当前档案并存活；跨档案编号、不存在、已故或缺席一律报错。
        - required=False（全体/资源类决策）：忽略客户端传入的目标，返回 None，
          效果按全体结算，前端回传谁都不会把全体效果收窄成单体。
        """
        if not required:
            return None
        if target_id is None:
            raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
        target = next((r for r in self.session.residents if r.id == target_id), None)
        if target is None:
            raise BunkerEngineError("目标居民不存在或不属于当前档案")
        if not target.alive:
            raise BunkerEngineError("目标居民已故，无法作为效果目标")
        return target

    def resolve_crisis(self, event_key, choice_key, target_id=None, token=None):
        """结算待处理危机。

        结算必须命中档案里唯一的待处理危机：事件、选项、单体目标都与存档绑定，
        既不能凭空伪造一场危机（无待处理危机时拒绝），也不能重复结算
        （结算后待处理危机被清除并留下幂等凭据，重放只返回上次结果）。
        返回 (detail, replayed)：replayed=True 表示这是重复请求，未再次施加效果。
        """
        self._ensure_running()
        pending, event = self._pending_event()

        # 已有同一危机（事件/选项/目标/发生日一致）的结算记录：
        # 重复提交（含并发落败方）只回放，不二次结算
        pending_day = pending.get("day") if pending else None
        if self._matches_resolution(
            self.session.last_resolution, event_key, choice_key, target_id, day=pending_day
        ):
            return self.session.last_resolution.get("detail", ""), True

        if pending is None:
            raise BunkerEngineError("当前没有待处理的危机，无法结算")

        # 事件必须与存档中的待处理危机一致：不能用 A 事件的请求去结算 B
        if event_key != pending.get("event"):
            raise BunkerEngineError("危机事件与当前待处理事件不符")
        # token 用于区分“同一危机上一次的旧点击”与刷新后恢复的当前决策；
        # 旧客户端/旧档案没有 token 时退化为仅按事件匹配
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该危机决策已过期，请按当前危机重新选择")

        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")

        effects = choice.get("effects", {})

        # 作用域由所选决策的效果声明决定，客户端传入的 target_id 不能改变它：
        # 单体效果必须携带有效目标，全体效果一律忽略客户端目标
        targeted = self._choice_targeted(choice)
        if targeted:
            # 目标与待处理危机绑定：不能用任意/其他居民编号替换事件目标
            bound_id = pending.get("target_id")
            if target_id is None:
                raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
            if bound_id is not None and target_id != bound_id:
                raise BunkerEngineError("目标居民与本次危机指定的幸存者不符")
        # 在应用任何效果前完成目标校验，保证失败时档案状态不发生部分变更
        target = self._resolve_target(target_id, required=targeted)

        detail_parts = []

        # 资源效果
        for k, v in effects.get("resources", {}).items():
            self._add_resource(k, v)
            detail_parts.append(f"{RESOURCE_ZH.get(k,k)} {v:+.0f}")
        # 健康/士气效果：single 只作用于目标本人，all 作用于全体存活者
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = [r for r in self.session.residents if r.alive]
                scope = "全体"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        if "add_resident" in effects:
            self._add_resident(effects["add_resident"])
            detail_parts.append(f"加入新幸存者 {effects['add_resident']}")
        if effects.get("trap"):
            detail_parts.append("（不良后果）")

        # 日志与实际结算同一作用域：单体写名，全体写明“全体幸存者”
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", event["title"], f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])

        # 清除待处理危机并记下幂等凭据——无论后续是否终局，本危机都已结算
        self.session.pending_crisis = None
        self.session.last_resolution = {
            "token": pending.get("token"),
            "event": event["key"],
            "choice": choice["key"],
            "target_id": target.id if targeted else None,
            "day": pending.get("day"),
            "detail": detail,
        }
        self._check_end()
        return detail, False

    def reconcile_stale_resolution(self, event_key, choice_key, target_id, token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次结算则安全回放。

        返回 (detail, replayed)；请求与任何已知结算都对不上时抛 409，
        由调用方提示“危机状态已变化”，杜绝并发重复结算。
        """
        rec = self.session.last_resolution
        if self._matches_resolution(rec, event_key, choice_key, target_id) and (
            token is None or not rec.get("token") or token == rec.get("token")
        ):
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("危机状态已被其他请求更新，请刷新后重试")

    def _add_resident(self, name):
        r = Resident(
            session_id=self.session.id,
            name=name,
            job="general",
            health=70.0,
            morale=60.0,
            alive=1,
            joined_day=self.session.day,
        )
        self.db.add(r)
        self.session.survivors += 1
        return r

    # ---- 外出探索 ----
    def _normalize_provisions(self, provisions):
        if provisions is None:
            provisions = {}
        if not isinstance(provisions, dict):
            raise BunkerEngineError("携带物资格式不正确")
        clean = {}
        for key, amount in provisions.items():
            if key not in RESOURCE_KEYS:
                raise BunkerEngineError(f"未知物资：{key}")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                raise BunkerEngineError("携带物资数量必须是数字")
            if amount < 0:
                raise BunkerEngineError("携带物资数量不能为负")
            if amount > 0:
                clean[key] = round(amount, 1)
        return clean

    def start_expedition(self, resident_ids, provisions=None, token=None):
        """派出探索队。物资立即离堡，遭遇写入档案，刷新后可恢复。"""
        self._require_daily_phase("派出探索队")
        provisions = self._normalize_provisions(provisions)
        active = self.active_expedition()
        if active:
            same_party = [p["id"] for p in active.get("party", [])] == list(resident_ids)
            same_provisions = active.get("provisions") == provisions
            if token and active.get("token") == token and same_party and same_provisions:
                return active
            raise BunkerEngineError("已有探索队在外，必须等待其归队后才能再次出发")
        if not resident_ids:
            raise BunkerEngineError("至少选择一名幸存者外出探索")
        if len(set(resident_ids)) != len(resident_ids):
            raise BunkerEngineError("同一名幸存者不能重复加入探索队")

        members = []
        away_ids = self.away_resident_ids()
        for rid in resident_ids:
            resident = next((r for r in self.session.residents if r.id == rid), None)
            if resident is None:
                raise BunkerEngineError("探索队员不存在或不属于当前档案")
            if resident.id in away_ids or not resident.alive:
                raise BunkerEngineError(f"{resident.name} 当前无法外出探索")
            members.append(resident)

        required = {}
        for member in members:
            for key, amount in EXPEDITION_PROVISION_PER_PERSON.items():
                required[key] = required.get(key, 0.0) + amount
        for key, amount in required.items():
            if self.get_resources().get(key, 0.0) < amount:
                raise BunkerEngineError(f"{RESOURCE_ZH[key]}不足以配发探索队的最低补给")
        if not self._can_afford(provisions):
            raise BunkerEngineError("资源不足，无法携带所选物资")

        # 先完成全部校验再扣物资：失败请求不产生部分离库
        for key, amount in required.items():
            self._add_resource(key, -amount)
        for key, amount in provisions.items():
            self._add_resource(key, -amount)

        carried = {k: round(required.get(k, 0.0) + provisions.get(k, 0.0), 1) for k in RESOURCE_KEYS}
        expedition = {
            "token": token or uuid.uuid4().hex,
            "started_day": self.session.day,
            "depth": 0,
            "returning": False,
            "party": [
                {"id": r.id, "name": r.name, "health": r.health, "morale": r.morale, "alive": 1}
                for r in members
            ],
            "carried": carried,
            "provisions": provisions,
            "loot": {k: 0.0 for k in RESOURCE_KEYS},
            "rescued": [],
            "log": [f"{self.session.day}天：{ '、'.join(r.name for r in members) }携补给离堡"],
            "pending_encounter": None,
            "last_action": None,
        }
        self.session.active_expedition = expedition
        names = "、".join(r.name for r in members)
        carry_text = "，".join(
            f"{RESOURCE_ZH[k]} {v:.0f}" for k, v in carried.items() if v > 0
        )
        self._log("expedition", "探索队离堡", f"{names} 携带{carry_text}外出。", decision="派出探索")
        return expedition

    def _begin_expedition_encounter(self):
        """在外推进一天后生成下一场遭遇；全员阵亡或已返程则等待归队。"""
        expedition = self.active_expedition()
        if not expedition or expedition.get("returning"):
            return None
        if not [p for p in expedition["party"] if p.get("alive", 1)]:
            expedition["returning"] = True
            expedition["pending_encounter"] = None
            return None
        event = self.rand.choice(EXPEDITION_POOL)
        target = self.rand.choice([p for p in expedition["party"] if p.get("alive", 1)])
        encounter = {
            "token": uuid.uuid4().hex,
            "event": event["key"],
            "day": self.session.day,
            "depth": expedition.get("depth", 0) + 1,
            "title": event["title"],
            "desc": event["desc"],
            "target_id": target["id"],
            "target_name": target["name"],
            "choices": [
                {"key": c["key"], "label": c["label"], "hint": c.get("hint", "")}
                for c in event["choices"]
            ],
        }
        expedition["pending_encounter"] = encounter
        return encounter

    def resolve_expedition_encounter(self, action_key, token=None):
        """处理一场探索遭遇，并由该动作决定继续搜索还是返程。"""
        self._ensure_running()
        expedition = self.active_expedition()
        if not expedition:
            raise BunkerEngineError("当前没有外出中的探索队")
        encounter = expedition.get("pending_encounter")
        if not encounter:
            raise BunkerEngineError("当前没有待处理的探索遭遇")

        last = expedition.get("last_action")
        if last and last.get("encounter_token") == encounter.get("token"):
            if last.get("action") != action_key:
                raise BunkerEngineError("这场遭遇已经做出抉择，不能改选其他行动")
            if token is not None and encounter.get("token") and token != encounter["token"]:
                raise BunkerEngineConflict("该探索遭遇已过期，请按当前遭遇重新选择")
            return expedition, last.get("detail", ""), True

        if token is not None and encounter.get("token") and token != encounter["token"]:
            raise BunkerEngineConflict("该探索遭遇已过期，请按当前遭遇重新选择")

        event = next((e for e in EXPEDITION_POOL if e["key"] == encounter["event"]), None)
        if event is None:
            raise BunkerEngineError("探索遭遇已失效，请刷新档案后重试")
        action = next((c for c in event["choices"] if c["key"] == action_key), None)
        if action is None:
            raise BunkerEngineError("未知探索行动")

        target = next((p for p in expedition["party"] if p["id"] == encounter["target_id"]), None)
        if target is None or not target.get("alive", 1):
            raise BunkerEngineError("遭遇绑定的探索队员已无法行动")

        detail_parts = []
        guaranteed = action.get("effects", {})

        if action.get("rescue"):
            detail_parts.append("分出全队口粮和饮水")

        for key, amount in guaranteed.get("resources", {}).items():
            if amount >= 0:
                expedition["loot"][key] = round(expedition["loot"].get(key, 0.0) + amount, 1)
                detail_parts.append(f"获得{RESOURCE_ZH[key]} {amount:.0f}")
            else:
                self._consume_expedition_resource(expedition, key, -amount, detail_parts)

        morale = guaranteed.get("morale", 0)
        if morale:
            for member in expedition["party"]:
                if member.get("alive", 1):
                    member["morale"] = round(_clamp(member["morale"] + morale), 1)
            detail_parts.append(f"士气 {morale:+.0f}（全队）")

        health = guaranteed.get("health", 0)
        if health:
            target["health"] = round(_clamp(target["health"] + health), 1)
            if target["health"] <= 0:
                target["alive"] = 0
            detail_parts.append(f"健康 {health:+.0f}（{target['name']}）")

        risk = action.get("risk")
        if risk and self.rand.random() < risk.get("chance", 0):
            damage = risk.get("health", 0)
            target["health"] = round(_clamp(target["health"] - damage), 1)
            if target["health"] <= 0:
                target["alive"] = 0
                target["health"] = 0
            detail_parts.append(f"险情发生：{target['name']} 健康 -{damage:.0f}")

        if action.get("rescue"):
            rescued_name = f"流浪者{len(expedition.get('rescued', [])) + 1}"
            expedition.setdefault("rescued", []).append(
                {"name": rescued_name, "health": 65.0, "morale": 55.0}
            )
            detail_parts.append(f"救下{rescued_name}")

        expedition["depth"] = encounter["depth"]
        turning_back = bool(action.get("return")) or expedition["depth"] >= EXPEDITION_MAX_DEPTH
        if turning_back:
            expedition["returning"] = True
            expedition["pending_encounter"] = None
            detail_parts.append("探索队决定返程" if action.get("return") else "已达最远搜索距离，自动返程")
        else:
            expedition["pending_encounter"] = None

        detail = "，".join(detail_parts) if detail_parts else "无显著收获"
        action_log = (
            f"{encounter['day']}天 · {event['title']}：选择「{action['label']}」，{detail}"
        )
        expedition.setdefault("log", []).append(action_log)
        expedition["last_action"] = {
            "encounter_token": encounter["token"],
            "action": action_key,
            "detail": detail,
            "turning_back": turning_back,
        }
        self._log("expedition", event["title"], f"{action_log}（目标：{target['name']}）", decision=action["label"])
        return expedition, detail, False

    def _settle_expedition(self):
        """归队当天统一写回健康/士气、死亡、获救者与战利品，只结算一次。"""
        expedition = self.active_expedition()
        if not expedition:
            return None

        dead_names, alive_party = [], []
        for member in expedition["party"]:
            resident = next((r for r in self.session.residents if r.id == member["id"]), None)
            if resident is None:
                continue
            resident.health = member.get("health", resident.health)
            resident.morale = member.get("morale", resident.morale)
            if member.get("alive", 1) and resident.health > 0:
                alive_party.append(resident)
            else:
                resident.alive = 0
                resident.health = 0
                self.session.survivors -= 1
                dead_names.append(resident.name)

        loot_text = []
        if alive_party:
            for key, amount in expedition.get("loot", {}).items():
                if amount > 0:
                    self._add_resource(key, amount)
                    loot_text.append(f"{RESOURCE_ZH[key]} +{amount:.0f}")
            for rescued in expedition.get("rescued", []):
                newcomer = self._add_resident(rescued["name"])
                self.db.flush()
                newcomer.health = rescued.get("health", 65.0)
                newcomer.morale = rescued.get("morale", 55.0)
        else:
            for rescued in expedition.get("rescued", []):
                dead_names.append(rescued["name"])
            loot_text.append("全员未能归队，战利品遗失")

        names = "、".join(r.name for r in alive_party) or "无人"
        title = "探索队归队"
        parts = [f"归队：{names}"]
        if dead_names:
            parts.append(f"伤亡：{'、'.join(dead_names)}")
        rescued_names = [x["name"] for x in expedition.get("rescued", [])]
        if alive_party and rescued_names:
            parts.append(f"获救加入：{'、'.join(rescued_names)}")
        if loot_text:
            parts.append("，".join(loot_text))
        detail = "；".join(parts)
        expedition.setdefault("log", []).append(f"{self.session.day}天：{detail}")
        self._log("expedition", title, detail, decision="归队结算")
        self.session.active_expedition = None
        return {"detail": detail, "dead": dead_names, "returned": [r.name for r in alive_party]}

    # ---- 扩建 ----
    def build_facility(self, category):
        self._require_daily_phase("建造设施")
        cost = FACILITY_COST[1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法建造")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f = Facility(
            session_id=self.session.id,
            name=FACILITY_ZH.get(category, category),
            category=category,
            level=1,
            status="active",
            built_day=self.session.day,
        )
        self.db.add(f)
        self.db.flush()  # 让新设施立即反映到 session.facilities 集合
        self._log("system", "设施扩建", f"建造了{FACILITY_ZH.get(category, category)}。", decision="扩建")
        return f

    def upgrade_facility(self, facility_id):
        self._require_daily_phase("升级设施")
        f = next((x for x in self.session.facilities if x.id == facility_id), None)
        if not f:
            raise BunkerEngineError("设施不存在")
        if f.level >= max(FACILITY_COST.keys()):
            raise BunkerEngineError("已达最高等级")
        cost = FACILITY_COST[f.level + 1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法升级")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f.level += 1
        self._log("system", "设施升级", f"{FACILITY_ZH.get(f.category, f.category)} 提升到 Lv.{f.level}。", decision="升级")
        return f

    def _can_afford(self, cost):
        res = self.get_resources()
        return all(res.get(k, 0) >= v for k, v in cost.items())

    # ---- 任务分配（重分配岗位）----
    def set_job(self, resident_id, job):
        self._require_daily_phase("调整岗位")
        if job not in JOB_EFFICIENCY:
            raise BunkerEngineError("未知岗位")
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        if r.id in self.away_resident_ids():
            raise BunkerEngineError("该居民正在外出探索，归队后才能调整岗位")
        r.job = job

    # ---- 结局判定 ----
    def _check_end(self):
        if self.session.status != "running":
            return True
        # 胜利：存活达到目标天数
        if self.session.day >= self.session.target_day:
            self._finish(win=True, reason=f"坚持到第{self.session.day}天，末日阴影散去，幸存者们走向了新生。")
            return True
        # 失败：人口归零
        if self.session.survivors <= 0:
            self._finish(win=False, reason="所有幸存者都已逝去，地堡陷入永恒的寂静。")
            return True
        # 失败：四资源全线崩溃；探索队尚未归队时仍可能带回补给，暂不提前终局
        if not self.active_expedition():
            res = self.get_resources()
            if all(res.get(k, 0) <= 1 for k in RESOURCE_KEYS):
                self._finish(win=False, reason="食物、水源、电力和氧气全线枯竭，地堡无法再维系生命。")
                return True
        return False

    def _finish(self, win, reason):
        self.session.status = "win" if win else "over"
        # 进入终局后不存在悬而未决的危机/探索，状态机统一收敛到 ended
        self.session.pending_crisis = None
        self.session.active_expedition = None
        alive = [r for r in self.session.residents if r.alive]
        # 计分：幸存者 * 天数 * 士气系数
        morale = self.avg_morale()
        score = int(self.session.survivors * self.session.day * (0.5 + morale / 200.0))
        self.session.score = score
        self.session.outcome = {"win": win, "reason": reason, "survivors": len(alive), "day": self.session.day}
        self._log("system", "游戏结束", reason, decision="结局")


RESOURCE_ZH = {"food": "食物", "water": "水源", "power": "电力", "oxygen": "氧气"}
FACILITY_ZH = {"farm": "穹顶菜园", "water": "净水器", "power": "发电机", "oxygen": "水培制氧", "med": "医疗舱", "storage": "仓储区"}


# ============ 危机事件池（决策树） ============
CRISIS_POOL = [
    {
        "key": "radstorm",
        "title": "辐射风暴来袭",
        "desc": "一场强辐射风暴正在逼近地堡。派工程师抢修屏蔽层，或让所有人避难并停电。",
        "choices": [
            {
                "key": "shield_repair",
                "label": "抢修屏蔽层",
                "hint": "消耗少量电力，成功则平安，失败有人员受伤",
                "effects": {"resources": {"power": -8}},
            },
            {
                "key": "shutdown",
                "label": "全员断电避难",
                "hint": "所有设施停摆一天，电力下降，无人员风险",
                "effects": {"resources": {"power": -15, "food": -5, "water": -4}},
            },
        ],
    },
    {
        "key": "mutiny",
        "title": "地堡内讧",
        "desc": "因食物分配不公，一部分人情绪失控，要求重新分配口粮。",
        "choices": [
            {
                "key": "double_ration",
                "label": "加倍发放食物",
                "hint": "士气+20，但食物储备大减",
                "effects": {"resources": {"food": -20}, "morale": 20},
            },
            {
                "key": "suppress",
                "label": "严令镇压",
                "hint": "食物不变，但士气大降",
                "effects": {"morale": -15},
            },
        ],
    },
    {
        "key": "leak",
        "title": "氧气泄漏",
        "desc": "水培舱密封圈老化，氧气正在泄漏。",
        "choices": [
            {
                "key": "emergency_repair",
                "label": "紧急封堵",
                "hint": "消耗食物与电力，防止气体外泄",
                "effects": {"resources": {"food": -6, "power": -6}},
            },
            {
                "key": "vent",
                "label": "先泄压再修",
                "hint": "氧气大降但更省资源",
                "effects": {"resources": {"oxygen": -20, "power": -3}},
            },
        ],
    },
    {
        "key": "sick",
        "title": "疫病袭来",
        "desc": "一名幸存者出现不明高热，可能是污染引发的疾病。",
        "choices": [
            {
                "key": "quarantine",
                "label": "隔离治疗",
                "hint": "该居民卸下工作，健康缓慢回复",
                "effects": {"resources": {"food": -4}, "health": {"value": -5, "target": "single"}},
            },
            {
                "key": "public_health",
                "label": "全员消毒",
                "hint": "消耗电力与水源消毒，保护大家",
                "effects": {"resources": {"power": -6, "water": -8}},
            },
        ],
    },
    {
        "key": "raid",
        "title": "盗匪袭扰",
        "desc": "地堡外传来敲击声，一伙流民试图破门而入抢夺物资。",
        "choices": [
            {
                "key": "defend",
                "label": "武装抵抗",
                "hint": "能耗物资，可能有人受伤，但守住粮食",
                "effects": {"resources": {"food": -2, "power": -4}, "health": {"value": -8, "target": "single"}},
            },
            {
                "key": "bribe",
                "label": "分粮和解",
                "hint": "交出部分食物换取平安",
                "effects": {"resources": {"food": -18}},
            },
        ],
    },
    {
        "key": "scavenge",
        "title": "发现物资舱",
        "desc": "侦察队在地堡深处发现一间废弃补给舱，但已部分损坏。",
        "choices": [
            {
                "key": "crack_open",
                "label": "强制开启",
                "hint": "可能获得大量补给，也可能毁坏",
                "effects": {"resources": {"food": 12, "water": 8}},
            },
            {
                "key": "careful",
                "label": "小心拆解",
                "hint": "稳定获得少量补给",
                "effects": {"resources": {"food": 6, "water": 5, "power": 3}},
            },
        ],
    },
    {
        "key": "blizzard",
        "title": "暴雪封门",
        "desc": "极寒暴雪掩盖了地堡入口，通风与采能都受影响。",
        "choices": [
            {
                "key": "burn_fuel",
                "label": "燃烧燃料保温",
                "hint": "消耗食物(燃料)维持温度",
                "effects": {"resources": {"food": -10}},
            },
            {
                "key": "huddle",
                "label": "集中避寒",
                "hint": "士气下降，但省下燃料",
                "effects": {"morale": -10},
            },
        ],
    },
]


# ============ 外出探索遭遇池 ============
EXPEDITION_POOL = [
    {
        "key": "ruins",
        "title": "坍塌废墟",
        "desc": "小队在废城边缘发现一片坍塌的超市废墟，深处隐约可见补给货架。",
        "choices": [
            {
                "key": "careful_search",
                "label": "小心搜刮",
                "hint": "稳定获得少量食物和水，无人身风险",
                "effects": {"resources": {"food": 8, "water": 4}},
            },
            {
                "key": "deep_search",
                "label": "深入废墟",
                "hint": "大量物资，但可能被坠物击伤",
                "effects": {"resources": {"food": 18, "water": 8, "power": 6}},
                "risk": {"chance": 0.35, "health": 20},
            },
            {
                "key": "turn_back",
                "label": "放弃搜索，立即返程",
                "hint": "不追加风险，携带已有收获回堡",
                "return": True,
            },
        ],
    },
    {
        "key": "feral_dogs",
        "title": "野犬环伺",
        "desc": "一群饥饿的野犬堵住了巷道，犬群后方有一具旧探险队的背包。",
        "choices": [
            {
                "key": "slip_away",
                "label": "悄声绕行",
                "hint": "获得少量补给，但仍可能被发现",
                "effects": {"resources": {"food": 4}},
                "risk": {"chance": 0.25, "health": 15},
            },
            {
                "key": "drive_off",
                "label": "驱散犬群",
                "hint": "可能受伤，成功后取得背包",
                "effects": {"resources": {"food": 14, "oxygen": 6}, "morale": 3},
                "risk": {"chance": 0.5, "health": 25},
            },
            {
                "key": "turn_back",
                "label": "避免交战，立即返程",
                "hint": "保全队伍，携带已有收获回堡",
                "return": True,
            },
        ],
    },
    {
        "key": "stranded_survivor",
        "title": "求救信号",
        "desc": "废弃地铁站里传来敲击声，一名流浪者被困在卷帘门后。",
        "choices": [
            {
                "key": "rescue",
                "label": "分出口粮并施救",
                "hint": "消耗全队口粮和水，可能带回新居民并鼓舞士气",
                "rescue": True,
                "effects": {"morale": 8},
            },
            {
                "key": "leave_supplies",
                "label": "留下路线图继续搜索",
                "hint": "无法确认对方能否脱险，队伍继续前进",
                "effects": {"morale": -4},
            },
            {
                "key": "turn_back",
                "label": "无力施救，立即返程",
                "hint": "不消耗额外补给，全队返程",
                "return": True,
                "effects": {"morale": -2},
            },
        ],
    },
    {
        "key": "sealed_cache",
        "title": "密封物资舱",
        "desc": "山壁间嵌着战前应急物资舱，手动锁已经锈蚀，旁边的氧烛仍可回收。",
        "choices": [
            {
                "key": "slow_unlock",
                "label": "缓慢拆锁",
                "hint": "稳定取得水和氧气，无人员风险",
                "effects": {"resources": {"water": 8, "oxygen": 5}},
            },
            {
                "key": "force_hatch",
                "label": "撬开舱门",
                "hint": "电力与氧气更多，但舱门可能反弹伤人",
                "effects": {"resources": {"power": 10, "oxygen": 10}},
                "risk": {"chance": 0.4, "health": 18},
            },
            {
                "key": "turn_back",
                "label": "放弃物资舱，立即返程",
                "hint": "携带已有收获安全回堡",
                "return": True,
            },
        ],
    },
]
