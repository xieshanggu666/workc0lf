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

# 探索队系统
EXPEDITION_SUPPLY_PER_DAY = {FOOD: 1.0, WATER: 1.0}  # 每人每日消耗自带物资
EXPEDITION_MAX_DAYS = 7       # 最长探索天数，期满强制返程
EXPEDITION_ENCOUNTER_CHANCE = 0.85  # 每日行军遭遇概率
EXPEDITION_MAX_MEMBERS = 4    # 每支探索队上限


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
#   daily  —— 每日阶段，可建造/升级/调岗，可推进一天
#   crisis —— 危机阶段，存在待处理危机，除结算危机外拒绝一切推进与经营动作
#   ended  —— 终局（win/over），拒绝任何状态变更
PHASE_DAILY, PHASE_CRISIS, PHASE_ENDED = "daily", "crisis", "ended"
# 探索阶段：探索队在外且存在待处理遭遇，状态机拒绝一切经营/推进动作
PHASE_EXPEDITION = "expedition"


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
        exp = self.session.expedition
        if exp and exp.get("status") == "away" and exp.get("pending_encounter"):
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
        away = self._away_resident_ids()
        return sum(1 for r in self.session.residents if r.alive and r.job == job and r.id not in away)

    def active_facilities(self):
        return [f for f in self.session.facilities if f.status == "active"]

    # ---- 探索队成员追踪 ----
    def _away_resident_ids(self):
        """当前探索队编制内的居民编号（无论生死）；无在外队伍时为空集。"""
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            return set()
        return set(exp.get("members", []))

    def _away_residents(self):
        """探索队编制内的全部居民（含已阵亡，用于返程结算）。"""
        ids = self._away_resident_ids()
        return [r for r in self.session.residents if r.id in ids]

    def _in_bunker_residents(self):
        """地堡内存活居民（排除探索队成员）。"""
        away = self._away_resident_ids()
        return [r for r in self.session.residents if r.alive and r.id not in away]

    def _in_bunker_count(self):
        return len(self._in_bunker_residents())

    # ---- 每日推进 ----
    def advance_day(self):
        # 终局或存在待处理抉择（危机/探索遭遇）时都不能推进：抉择不可被"再点一天"跳过
        self._require_phase(PHASE_DAILY, "存在待处理抉择，必须先完成才能推进")
        self.session.day += 1
        self._apply_production_and_consumption()
        self._apply_health_morale()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            # 探索队在外出差：地堡按在堡人口结算，探索队消耗自带物资、行军并触发遭遇
            self._apply_expedition_travel(exp)
            # 强制返程（补给耗尽/期满/全员失联）会清除探索队状态：
            # 此时不得再用旧 exp 触发遭遇，否则会把已结算的队伍恢复成"在外"
            if self.session.expedition is None:
                if self._check_end():
                    return None
                return None
            if self._check_end():
                return None
            # 探索队行军中：触发遭遇（替代地堡危机），遭遇挂起后进入 expedition 阶段
            return self._maybe_trigger_expedition_encounter(exp)
        # 终局优先：抵达目标日或全面崩溃直接结算结局，不再凭空挂起一个
        # 永远无法处理的危机（统一每日推进 → 危机处理 → 终局的流转）
        if self._check_end():
            return None
        return self._maybe_trigger_crisis()

    def _apply_production_and_consumption(self):
        # 离堡人员不消耗地堡物资（吃自带口粮），地堡消耗只计在堡人口
        pop = self._in_bunker_count()
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
            f"人口{pop}，食物净变{round(consume[FOOD]-prod[FOOD],1):+}、水{round(consume[WATER]-prod[WATER],1):+}、电力{round(consume[POWER]-prod[POWER],1):+}、氧气{round(consume[OXY]-prod[OXY],1):+}",
            decision="例行更新",
        )

    def _apply_health_morale(self):
        res = self.get_resources()
        away = self._away_resident_ids()
        # 资源不足影响（仅作用于在堡居民；探索队吃自带物资，不受地堡短缺波及）
        for r in self.session.residents:
            if not r.alive:
                continue
            if r.id in away:
                continue
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
        # 每日最多因匮乏死 1 人，依次从在堡最弱居民开始（探索队不在堡内，不参与地堡匮乏判定）
        alive = self._in_bunker_residents()
        if not alive:
            return
        weakest = min(alive, key=lambda r: r.health)
        weakest.alive = 0
        weakest.health = 0
        self.session.survivors -= 1
        self._log("crisis", "生存危机：资源耗尽", f"{weakest.name} 因匮乏失去生命。", decision="自然事件")

    def avg_morale(self):
        alive = [r for r in self.session.residents if r.alive]
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
        alive = [r for r in self.session.residents if r.alive]
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
        """经营/推进类动作只允许在每日阶段执行。"""
        self._ensure_running()
        if self.phase == PHASE_CRISIS:
            raise BunkerEngineError(f"存在待处理危机，必须先完成抉择才能{action}")

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

    # ---- 探索队 ----
    def _random_survivor_name(self):
        import random
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许")
        givens = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华玉萍红斌")
        return random.choice(surnames) + random.choice(givens)

    def send_expedition(self, member_ids, supplies):
        """派遣探索队：选择在堡居民并分配自带物资，队伍出发后暂停地堡生产。

        离堡人员不参与设施产出、不消耗地堡口粮；行军消耗自带物资，
        途中遭遇由玩家抉择，返程时统一结算战利品与伤亡。
        """
        self._ensure_running()
        if self.phase != PHASE_DAILY:
            raise BunkerEngineError("当前状态无法派遣探索队")
        if self.session.expedition:
            raise BunkerEngineError("已有探索队在外，无法同时派遣第二支队伍")
        if not member_ids:
            raise BunkerEngineError("必须选择至少一名居民参加探索队")
        if len(member_ids) > EXPEDITION_MAX_MEMBERS:
            raise BunkerEngineError(f"探索队最多 {EXPEDITION_MAX_MEMBERS} 人")
        # 校验队员：必须是在堡存活居民
        members = []
        for mid in member_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("队员不存在或已故，无法参加探索队")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已在探索队中")
            members.append(r)
        # 校验并扣除自带物资
        supply_cost = {}
        for k, v in (supplies or {}).items():
            if k not in RESOURCE_KEYS:
                raise BunkerEngineError(f"未知物资 {k}")
            if v < 0:
                raise BunkerEngineError("物资数量不能为负")
            supply_cost[k] = float(v)
        if not self._can_afford(supply_cost):
            raise BunkerEngineError("物资不足，无法派遣")
        for k, v in supply_cost.items():
            self._add_resource(k, -v)
        # 写入探索队快照（含一次性 token，刷新后恢复同一支队伍）
        exp = {
            "token": uuid.uuid4().hex,
            "status": "away",
            "started_day": self.session.day,
            "members": [r.id for r in members],
            "supplies": dict(supply_cost),
            "travel_days": 0,
            "encounters_resolved": 0,
            "pending_encounter": None,
            "loot": {},
            "casualties": [],
            "last_return": None,
        }
        self.session.expedition = dict(exp)
        names = "、".join(r.name for r in members)
        self._log("system", "探索队出发", f"{names} 携带物资外出探索。", decision="派遣探索队")
        return exp

    def _apply_expedition_travel(self, exp):
        """探索队每日行军：消耗自带物资、累计天数，触发强制返程判定。"""
        alive_members = [r for r in self._away_residents() if r.alive]
        if not alive_members:
            # 全员失联：强制返程（无人生还）
            self._settle_expedition(exp, reason="探索队全员失联")
            return
        exp["travel_days"] = exp.get("travel_days", 0) + 1
        # 消耗自带口粮
        n = len(alive_members)
        supplies = exp.get("supplies", {})
        for k in (FOOD, WATER):
            cost = EXPEDITION_SUPPLY_PER_DAY[k] * n
            supplies[k] = round(supplies.get(k, 0.0) - cost, 1)
        exp["supplies"] = supplies
        # 物资耗尽或达到最长探索天数：强制返程
        if supplies.get(FOOD, 0) <= 0 or supplies.get(WATER, 0) <= 0:
            self._settle_expedition(exp, reason="补给耗尽，探索队被迫返程")
            return
        if exp["travel_days"] >= EXPEDITION_MAX_DAYS:
            self._settle_expedition(exp, reason="探索期满，探索队返程")
            return
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)

    def _maybe_trigger_expedition_encounter(self, exp):
        """每日行军后概率触发遭遇；已有待处理遭遇时不重复触发。"""
        if exp.get("pending_encounter"):
            return exp["pending_encounter"]
        if self.rand.random() > EXPEDITION_ENCOUNTER_CHANCE:
            return None
        event = self.rand.choice(EXPEDITION_ENCOUNTERS)
        encounter = self._build_expedition_encounter(event, exp)
        exp["pending_encounter"] = encounter
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)
        return encounter

    def _build_expedition_encounter(self, event, exp):
        alive_members = [r for r in self._away_residents() if r.alive]
        # 仅当存在单体健康效果的决策时才随机目标队员
        needs_target = any(
            self._choice_targeted(c) for c in event["choices"]
        )
        target = self.rand.choice(alive_members) if needs_target and alive_members else None
        return {
            "token": uuid.uuid4().hex,
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

    def resolve_expedition_encounter(self, choice_key, token=None):
        """处理探索队途中遭遇：抉择影响队员健康/士气、物资与战利品。

        结算必须命中央档案里唯一的待处理遭遇：事件、选项、单体目标都与存档绑定，
        token 用于识别过期/重复请求；结算后待处理遭遇被清除，重复提交只回放。
        """
        self._ensure_running()
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            raise BunkerEngineError("当前没有在外的探索队")
        pending = exp.get("pending_encounter")
        if not pending:
            raise BunkerEngineError("当前没有待处理的探索遭遇")
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该遭遇决策已过期，请刷新后重试")
        event_key = pending.get("event")
        event = next((e for e in EXPEDITION_ENCOUNTERS if e["key"] == event_key), None)
        if not event:
            raise BunkerEngineError("探索遭遇已失效，请刷新档案后重试")
        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")
        effects = choice.get("effects", {})
        # 单体目标校验：必须是队内存活队员，且与待处理遭遇绑定
        targeted = self._choice_targeted(choice)
        target = None
        if targeted:
            bound_id = pending.get("target_id")
            if bound_id is None:
                raise BunkerEngineError("该决策需要指定一名队员作为目标")
            target = next((r for r in self._away_residents() if r.id == bound_id), None)
            if not target or not target.alive:
                raise BunkerEngineError("目标队员不在队中或已故，无法作为效果目标")
        # 在应用任何效果前完成校验，保证失败时档案状态不发生部分变更
        detail_parts = []
        alive_members = [r for r in self._away_residents() if r.alive]
        # 战利品（单独累计，返程时统一入库）
        loot = exp.get("loot", {})
        for k, v in effects.get("loot", {}).items():
            loot[k] = round(loot.get(k, 0.0) + v, 1)
            detail_parts.append(f"战利品 {RESOURCE_ZH.get(k, k)} +{v:g}")
        exp["loot"] = loot
        # 物资损失（从探索队自带物资中扣除）
        supplies = exp.get("supplies", {})
        for k, v in effects.get("supply_loss", {}).items():
            supplies[k] = round(supplies.get(k, 0.0) - v, 1)
            detail_parts.append(f"物资损失 {RESOURCE_ZH.get(k, k)} -{v:g}")
        exp["supplies"] = supplies
        # 健康/士气：单体作用于目标队员，全体作用于队内存活者
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = alive_members
                scope = "全体队员"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
                if r.health <= 0 and r.alive:
                    r.alive = 0
                    r.health = 0
                    if r.id not in exp.get("casualties", []):
                        exp["casualties"].append(r.id)
                        self.session.survivors = max(0, self.session.survivors - 1)
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        # 偶遇幸存者加入队伍
        if effects.get("add_resident"):
            name = self._random_survivor_name()
            self.db.flush()
            r = Resident(
                session_id=self.session.id, name=name, job="general",
                health=60.0, morale=50.0, alive=1, joined_day=self.session.day,
            )
            self.db.add(r)
            self.db.flush()  # 取得新居民 id
            exp["members"].append(r.id)
            self.session.survivors += 1
            detail_parts.append(f"新幸存者 {name} 加入队伍")
        # 日志与实际结算同一作用域
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"探索遭遇·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        # 清除待处理遭遇，队伍继续在外行军
        exp["pending_encounter"] = None
        exp["encounters_resolved"] = exp.get("encounters_resolved", 0) + 1
        self.session.expedition = dict(exp)
        return detail, False

    def return_expedition(self, token=None):
        """玩家主动召回探索队：结算战利品入库、伤亡扣减、剩余物资归还。

        返程必须命中央档案里唯一的在外探索队；token 用于识别过期/重复请求。
        结算后探索队状态被清除并留下幂等凭据，重复提交只回放上次结果。
        """
        self._ensure_running()
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            raise BunkerEngineError("当前没有在外的探索队")
        if exp.get("pending_encounter"):
            raise BunkerEngineError("探索队还有未处理的遭遇，无法返程")
        if token is not None and exp.get("token") and token != exp["token"]:
            raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
        return self._settle_expedition(exp, reason="探索队安全返程")

    def _settle_expedition(self, exp, reason):
        """结算探索队返程：战利品入库、剩余自带物资归还、伤亡扣减。

        幂等：以 token + 出发日为凭据，重复调用只回放，不二次发放战利品。
        返回 (detail, replayed)。
        """
        # 幂等回放：已有同一支队伍的返程结算记录
        last = exp.get("last_return")
        if last and last.get("token") == exp.get("token"):
            return last.get("detail", ""), True
        members = self._away_residents()
        dead_members = [r for r in members if not r.alive]
        # 战利品入库
        loot = exp.get("loot", {})
        loot_parts = [f"{RESOURCE_ZH.get(k, k)} +{v:g}" for k, v in loot.items() if v > 0]
        for k, v in loot.items():
            if v > 0:
                self._add_resource(k, v)
        # 剩余自带物资归还地堡
        supplies = exp.get("supplies", {})
        supply_parts = [f"剩余{RESOURCE_ZH.get(k, k)} +{round(v, 1):g}" for k, v in supplies.items() if v > 0]
        for k, v in supplies.items():
            if v > 0:
                self._add_resource(k, v)
        # 伤亡（阵亡队员已在遭遇结算时扣减过 survivors，此处不再重复扣减）
        casualty_names = [r.name for r in dead_members]
        # 组装日志
        detail_parts = []
        if loot_parts:
            detail_parts.append("战利品：" + "、".join(loot_parts))
        if supply_parts:
            detail_parts.append("归还物资：" + "、".join(supply_parts))
        if casualty_names:
            detail_parts.append(f"殉职：{'、'.join(casualty_names)}")
        else:
            detail_parts.append("全员平安归来")
        detail = "；".join(detail_parts)
        self._log("system", f"探索队返程（{reason}）", detail, decision="返程结算")
        # 记录幂等凭据并清除探索队状态
        exp["last_return"] = {"token": exp.get("token"), "day": self.session.day, "detail": detail}
        self.session.expedition = None
        self._check_end()
        return detail, False

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
        if r.id in self._away_resident_ids():
            raise BunkerEngineError("探索队中的居民无法调整岗位")
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
        # 失败：血量濒临且资源全面崩溃
        res = self.get_resources()
        if all(res.get(k, 0) <= 1 for k in RESOURCE_KEYS):
            self._finish(win=False, reason="食物、水源、电力和氧气全线枯竭，地堡无法再维系生命。")
            return True
        return False

    def _finish(self, win, reason):
        self.session.status = "win" if win else "over"
        # 进入终局后不存在悬而未决的危机，状态机统一收敛到 ended
        self.session.pending_crisis = None
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


# ============ 探索队遭遇池（外出探索途中的遭遇决策树） ============
# 与地堡危机相互独立：探索队在外时，每日行军触发的是探索遭遇而非地堡危机。
# 效果键：
#   loot       —— 战利品，单独累计，返程时统一入库
#   supply_loss —— 从探索队自带物资中扣除
#   health/morale —— 队员健康/士气（single 仅作用于目标队员，all 作用于全体队员）
#   add_resident —— 有幸存者加入队伍
EXPEDITION_ENCOUNTERS = [
    {
        "key": "cache",
        "title": "废弃补给点",
        "desc": "探索队在一处废墟中发现半埋的废弃补给箱，外观尚可辨认。",
        "choices": [
            {
                "key": "search_carefully",
                "label": "仔细搜索",
                "hint": "耗时但可能获得更多物资",
                "effects": {"loot": {FOOD: 8, WATER: 6}},
            },
            {
                "key": "grab_quickly",
                "label": "快速搜刮",
                "hint": "安全但收获有限",
                "effects": {"loot": {FOOD: 4, WATER: 3}},
            },
        ],
    },
    {
        "key": "beast",
        "title": "异兽袭击",
        "desc": "一头变异巨兽从废墟中窜出，挡住了去路。",
        "choices": [
            {
                "key": "fight",
                "label": "武装驱赶",
                "hint": "可能有人受伤，但能保住物资并缴获战利品",
                "effects": {"health": {"value": -12, "target": "single"}, "loot": {FOOD: 5}},
            },
            {
                "key": "flee",
                "label": "绕道撤退",
                "hint": "损失部分物资，但无人受伤",
                "effects": {"supply_loss": {FOOD: 6, WATER: 4}, "morale": -5},
            },
        ],
    },
    {
        "key": "weather",
        "title": "恶劣天气",
        "desc": "辐射尘暴骤起，能见度极低，探索队被迫寻找掩体。",
        "choices": [
            {
                "key": "take_shelter",
                "label": "就地躲避",
                "hint": "消耗一日物资，士气下降",
                "effects": {"supply_loss": {FOOD: 3, WATER: 3}, "morale": -8},
            },
            {
                "key": "push_through",
                "label": "冒雨前进",
                "hint": "可能生病，但不耽误行程",
                "effects": {"health": -6, "morale": -3},
            },
        ],
    },
    {
        "key": "survivors",
        "title": "偶遇幸存者",
        "desc": "探索队遇到一群流离失所的幸存者，他们请求加入地堡。",
        "choices": [
            {
                "key": "accept",
                "label": "接纳加入",
                "hint": "新增一名幸存者，但消耗更多补给",
                "effects": {"add_resident": True, "supply_loss": {FOOD: 4, WATER: 3}},
            },
            {
                "key": "trade",
                "label": "交换物资",
                "hint": "用自带物资换取情报与小份补给",
                "effects": {"supply_loss": {FOOD: 3}, "loot": {POWER: 5}, "morale": 3},
            },
            {
                "key": "refuse",
                "label": "拒绝并离开",
                "hint": "保持警惕，安然离开",
                "effects": {"morale": -2},
            },
        ],
    },
    {
        "key": "ruins",
        "title": "废墟探索",
        "desc": "一座保存较完整的废弃建筑矗立在眼前，隐约有物资的气息。",
        "choices": [
            {
                "key": "deep_explore",
                "label": "深入探索",
                "hint": "高风险高回报，可能有重大伤亡",
                "effects": {"loot": {FOOD: 12, WATER: 8, POWER: 6}, "health": {"value": -15, "target": "single"}},
            },
            {
                "key": "outer_search",
                "label": "外围搜索",
                "hint": "安全获得少量物资",
                "effects": {"loot": {FOOD: 5, WATER: 4}},
            },
        ],
    },
    {
        "key": "lost",
        "title": "迷路",
        "desc": "复杂的废墟巷道让探索队迷失了方向，补给在不知不觉中消耗。",
        "choices": [
            {
                "key": "retrace",
                "label": "凭记忆折返",
                "hint": "消耗额外物资寻找归路",
                "effects": {"supply_loss": {FOOD: 5, WATER: 4}, "morale": -5},
            },
            {
                "key": "climb_high",
                "label": "登高辨认",
                "hint": "冒险登高，可能有意外收获",
                "effects": {"loot": {FOOD: 3}, "health": -4, "morale": 2},
            },
        ],
    },
    {
        "key": "airdrop",
        "title": "空投补给",
        "desc": "一架老旧的运输机残骸旁，探索队发现了未被开启的空投舱。",
        "choices": [
            {
                "key": "open_carefully",
                "label": "小心开启",
                "hint": "稳定获得补给",
                "effects": {"loot": {FOOD: 6, WATER: 6, POWER: 4, OXY: 4}},
            },
            {
                "key": "force_open",
                "label": "强行破开",
                "hint": "可能获得更多，也可能损坏物资",
                "effects": {"loot": {FOOD: 10, WATER: 8, POWER: 6}, "supply_loss": {OXY: 3}},
            },
        ],
    },
    {
        "key": "trap",
        "title": "陷阱",
        "desc": "探索队触发了一处老旧的捕兽夹，一名队员被夹住。",
        "choices": [
            {
                "key": "free_carefully",
                "label": "小心解救",
                "hint": "可能加重伤势，但能保全物资",
                "effects": {"health": {"value": -10, "target": "single"}},
            },
            {
                "key": "force_free",
                "label": "强行挣脱",
                "hint": "伤势更重，但不耽误行程",
                "effects": {"health": {"value": -18, "target": "single"}, "supply_loss": {FOOD: 2}},
            },
        ],
    },
]