# -*- coding: utf-8 -*-
"""末日地堡生存 —— 核心引擎的可测试纯逻辑，验证资源守恒、危机决策、结局判定。

注意：测试使用独立内存级 Session，需清空表。为隔离，这里用 engine 建临时表。
"""
import pytest
from sqlalchemy.orm import Session

from app.core.database import Base, engine, SessionLocal
from app.core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from app.models import GameSession, Resident, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    CRISIS_POOL,
    FACILITY_ZH,
    FOOD,
    OXY,
    POWER,
    WATER,
)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def make_session(db, residents=3, resources=None):
    gs = GameSession(
        name="测试",
        day=1,
        target_day=SURVIVAL_TARGET_DAY,
        status="running",
        resources=resources or dict(INITIAL_RESOURCES),
        survivors=residents,
        score=0,
    )
    db.add(gs)
    db.flush()
    for i in range(residents):
        db.add(Resident(session_id=gs.id, name=f"人{i}", job="general", health=90, morale=80, alive=1, joined_day=1))
    for cat in ("power", "farm", "water", "oxygen"):
        db.add(Facility(session_id=gs.id, name=FACILITY_ZH[cat], category=cat, level=1, status="active", built_day=1))
    db.commit()
    db.refresh(gs)
    return gs


class FixedRand:
    """固定值随机 —— 每个 .random() 返回 0.9（不触发危机，因 0.9 > 0.45）。"""

    def random(self):
        return 0.9

    def choice(self, seq):
        return seq[0]


class TriggerRand(FixedRand):
    """必定触发危机（0.1 <= 0.45），事件取危机池第一项。"""

    def random(self):
        return 0.1


def arm_crisis(eng, event_key, target=None):
    """在档案上挂起一个待处理危机（模拟推进触发后等待抉择的状态）。"""
    event = next(e for e in CRISIS_POOL if e["key"] == event_key)
    crisis = eng._build_crisis(event)
    if target is not None:
        crisis["needs_target"] = True
        crisis["target_id"] = target.id
        crisis["target_name"] = target.name
    eng.session.pending_crisis = crisis
    return crisis


def test_advance_increments_day(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    assert gs.day == 2


def test_resources_change_with_population(db):
    """资源应有产出-消耗的净变化（守恒循环运行）。"""
    gs = make_session(db, residents=3)
    before = dict(gs.resources)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    after = gs.resources
    # 至少一个资源发生变化
    assert any(abs(after[k] - before[k]) > 0.01 for k in ("food", "water", "power", "oxygen"))


def test_build_deducts_cost(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    food_before = gs.resources[FOOD]
    eng.build_facility("med")
    assert gs.resources[FOOD] < food_before
    assert any(f.category == "med" for f in gs.facilities)


def test_build_fails_when_poor(db):
    gs = make_session(db)
    gs.resources = {FOOD: 1, WATER: 1, POWER: 1, OXY: 1}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.build_facility("farm")


def test_upgrade_increases_level(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    fac = [f for f in gs.facilities if f.category == "farm"][0]
    eng.upgrade_facility(fac.id)
    assert fac.level == 2


def test_crisis_applies_resource_effects(db):
    """选择翻倍食物选项应减食物。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 挂起待处理危机后结算（危机必须先经“推进触发”写入存档）
    event = CRISIS_POOL[0]
    arm_crisis(eng, event["key"])
    choice = event["choices"][0]
    eff = choice["effects"].get("resources", {}).get(FOOD, 0)
    food_before = gs.resources[FOOD]
    detail, replayed = eng.resolve_crisis(event["key"], choice["key"])
    assert replayed is False
    assert gs.resources[FOOD] <= food_before + eff + 1
    assert gs.pending_crisis is None


def test_job_assignment_changes_resident(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    r = gs.residents[0]
    eng.set_job(r.id, "farmer")
    assert r.job == "farmer"


def test_win_at_target_day(db):
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY  # 目标天数
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "win"


def test_population_zero_ends_game(db):
    gs = make_session(db)
    for r in gs.residents:
        r.alive = 0
    gs.survivors = 0
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "over"


def test_advance_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.advance_day()


def test_morale_recovery_toward_75(db):
    gs = make_session(db)
    for r in gs.residents:
        r.morale = 40
    gs.resources = {FOOD: 999, WATER: 999, POWER: 999, OXY: 999}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._apply_health_morale()
    assert all(r.morale > 40 for r in gs.residents)


# ---- 目标归属校验：防止跨档案数据污染 ----

def test_foreign_archive_target_rejected(db):
    """提交其他档案的居民编号：报错且本档案居民/资源均不受影响。"""
    gs = make_session(db)
    other = make_session(db)
    foreign_id = other.residents[0].id

    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 待处理疫病的目标已绑定为本档案某居民，跨档案编号必须被拒绝
    arm_crisis(eng, "sick", target=gs.residents[0])
    health_before = [r.health for r in gs.residents]
    food_before = gs.resources[FOOD]
    # 疫病·隔离：扣食物且对目标造成健康 -5
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=foreign_id)
    # 本档案无人受到伤害
    assert [r.health for r in gs.residents] == health_before
    # 目标校验在资源结算之前，资源也不应被扣减
    assert gs.resources[FOOD] == food_before
    # 失败结算不得清掉待处理危机，玩家仍需抉择
    assert gs.pending_crisis is not None


def test_nonexistent_target_rejected(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=gs.residents[0])
    health_before = [r.health for r in gs.residents]
    missing_id = max(r.id for r in gs.residents) + 9999
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=missing_id)
    assert [r.health for r in gs.residents] == health_before


def test_dead_target_rejected(db):
    gs = make_session(db)
    dead = gs.residents[0]
    dead.alive = 0
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=dead)
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=dead.id)


def test_valid_target_only_affects_that_resident(db):
    """有效本档案目标：健康效果只作用于其本人，不波及其他居民。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    target = gs.residents[1]
    arm_crisis(eng, "raid", target=target)
    others = [r for r in gs.residents if r.id != target.id]
    others_before = [r.health for r in others]
    # 盗匪·武装抵抗：健康 -8
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    assert target.health == 82  # 90 - 8
    assert [r.health for r in others] == others_before


def test_no_target_applies_to_all_alive(db):
    """未提供目标时，士气类全体效果仍按原语义作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 士气 +20
    assert all(r.morale == 100 for r in gs.residents if r.alive)


# ---- 前后端目标语义统一：作用域由事件效果声明，而非客户端回传 ----

def test_all_scope_crisis_carries_no_target(db):
    """内讧（纯全体士气事件）生成待决策时不应随机出目标。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    assert crisis["needs_target"] is False
    assert crisis["target_id"] is None
    assert crisis["target_name"] is None
    assert all(c["targeted"] is False for c in crisis["choices"])


def test_single_scope_crisis_carries_target_and_flags(db):
    """疫病存在单体决策，须随机目标；隔离=单人，全员消毒=全体。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "sick")
    assert crisis["needs_target"] is True
    assert crisis["target_id"] is not None
    flags = {c["key"]: c["targeted"] for c in crisis["choices"]}
    assert flags == {"quarantine": True, "public_health": False}


def test_global_morale_ignores_client_target(db):
    """回归：前端无条件回传随机目标时，全体士气决策仍须作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    random_target = gs.residents[0]
    others = [r for r in gs.residents if r.id != random_target.id]
    before = {r.id: r.morale for r in gs.residents}
    # 内讧·严令镇压：士气 -15（全体），即便带了目标编号也不应收窄
    eng.resolve_crisis("mutiny", "suppress", target_id=random_target.id)
    assert random_target.morale == before[random_target.id] - 15
    for r in others:
        assert r.morale == before[r.id] - 15


def test_global_scope_ignores_even_foreign_target(db):
    """全体效果不做目标校验：跨档案编号也不会让结算失败或作用于单人。"""
    gs = make_session(db)
    other = make_session(db)
    foreign_id = other.residents[0].id
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "suppress", target_id=foreign_id)
    assert all(r.morale == 65 for r in gs.residents if r.alive)


def test_single_scope_requires_target(db):
    """单体决策缺少目标时报错，且不产生任何部分结算。"""
    gs = make_session(db)
    food_before = gs.resources[FOOD]
    health_before = [r.health for r in gs.residents]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=gs.residents[0])
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=None)
    assert gs.resources[FOOD] == food_before
    assert [r.health for r in gs.residents] == health_before
    assert gs.pending_crisis is not None  # 失败结算不清除待处理危机


def test_single_vs_all_choice_scope_within_one_event(db):
    """同一疫病事件：隔离只伤目标，全员消毒不动任何人健康。"""
    gs = make_session(db)
    target = gs.residents[0]

    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=target)
    eng.resolve_crisis("sick", "quarantine", target_id=target.id)
    assert target.health == 85
    assert all(r.health == 90 for r in gs.residents if r.id != target.id)

    # 全员消毒：资源效果，无健康伤害，target_id 被忽略
    other = gs.residents[1]
    arm_crisis(eng, "sick", target=other)
    eng.resolve_crisis("sick", "public_health", target_id=other.id)
    assert other.health == 90
    assert target.health == 85  # 上一步的目标不受本次影响


def test_log_scope_matches_settlement(db):
    """日志作用域标注必须与实际结算一致：单体写姓名，全体写全体。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    target = gs.residents[1]
    arm_crisis(eng, "raid", target=target)
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")
    db.commit()

    logs = db.query(EventLog).filter_by(session_id=gs.id).order_by(EventLog.id).all()
    single_log = next(l for l in logs if "武装抵抗" in (l.detail or ""))
    global_log = next(l for l in logs if "加倍发放食物" in (l.detail or ""))
    assert target.name in single_log.detail
    assert "全体" in global_log.detail


def test_resource_change_persists_across_sessions(db):
    """资源 JSON 变更须真正落库（重新打开会话仍可见）。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    sid = gs.id
    before = gs.resources[FOOD]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 食物 -20
    db.commit()

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.resources[FOOD] == round(max(0.0, before - 20), 1)
    finally:
        db2.close()


# ---- 结算边界：已结束档案拒绝一切状态变更 ----

def test_actions_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    rid = gs.residents[0].id
    fid = gs.facilities[0].id
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=rid)
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")


# ---- 待处理危机入档：不可跳过、刷新恢复、绑定事件与目标 ----

def test_advance_blocked_while_crisis_pending(db):
    """危机待处理时推进一天必须被拒绝，且日期不前进、危机不被覆盖。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    assert crisis is not None
    day = gs.day
    token = crisis["token"]

    eng2 = BunkerEngine(db, gs, rand=TriggerRand())
    with pytest.raises(BunkerEngineError):
        eng2.advance_day()
    assert gs.day == day
    assert gs.pending_crisis["token"] == token  # 原有危机未被跳过/覆盖


def test_crisis_persisted_and_recoverable_after_reload(db):
    """待处理危机随存档落库，重新打开会话仍能恢复同一个决策。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    db.commit()
    sid, event, token, target_id = gs.id, crisis["event"], crisis["token"], crisis["target_id"]

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        pending = reloaded.pending_crisis
        assert pending is not None
        assert pending["event"] == event
        assert pending["token"] == token
        assert pending["target_id"] == target_id
        # 恢复后可正常完成结算
        eng2 = BunkerEngine(db2, reloaded, rand=FixedRand())
        choice_key = next(
            c["key"] for c in pending["choices"] if not c["targeted"]
        )
        eng2.resolve_crisis(event, choice_key, token=token)
        assert reloaded.pending_crisis is None
        db2.commit()
    finally:
        db2.close()


def test_resolve_without_pending_crisis_rejected(db):
    """没有待处理危机时凭空提交任意事件结算：拒绝且资源不变。"""
    gs = make_session(db)
    food_before = gs.resources[FOOD]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("scavenge", "crack_open")
    assert gs.resources[FOOD] == food_before


def test_resolve_wrong_event_rejected(db):
    """挂起的是 A 事件，提交 B 事件的抉择：拒绝且不结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    food_before = gs.resources[FOOD]
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("scavenge", "crack_open")
    assert gs.resources[FOOD] == food_before
    assert gs.pending_crisis["event"] == "mutiny"


def test_target_bound_to_pending_crisis(db):
    """单体决策的目标不可替换成其他居民。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    bound = gs.residents[0]
    other = gs.residents[1]
    arm_crisis(eng, "sick", target=bound)
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=other.id)
    assert bound.health == 90 and other.health == 90
    assert gs.pending_crisis is not None


def test_duplicate_resolve_settles_once(db):
    """同一抉择重复提交：第二次为幂等回放，效果只施加一次、日志只有一条。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    before = gs.resources[FOOD]

    detail1, replay1 = eng.resolve_crisis(
        "mutiny", "double_ration", token=crisis["token"]
    )
    after_first = gs.resources[FOOD]
    assert replay1 is False
    assert after_first == round(before - 20, 1)

    # 紧接着重复提交（同一引擎/同一事务内模拟用户连点）
    detail2, replay2 = eng.resolve_crisis(
        "mutiny", "double_ration", token=crisis["token"]
    )
    assert replay2 is True
    assert detail2 == detail1
    assert gs.resources[FOOD] == after_first  # 没有第二次扣减

    db.commit()
    logs = db.query(EventLog).filter_by(session_id=gs.id).count()
    assert logs == 1  # 只写了一条危机日志


def test_different_choice_after_resolve_rejected(db):
    """结算完成后改用另一选项再次提交：不得二次结算，直接拒绝。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    morale_after = gs.residents[0].morale
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "suppress", token=crisis["token"])
    assert gs.residents[0].morale == morale_after  # 未追加 -15


def test_same_event_next_day_is_not_a_replay(db):
    """第二天又触发同类型危机时，新结算不得被前一天的幂等记录拦截。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())

    c1 = arm_crisis(eng, "mutiny")
    gs.day = 5
    c1["day"] = 5
    food_d5 = gs.resources[FOOD]
    eng.resolve_crisis("mutiny", "double_ration", token=c1["token"])
    assert gs.resources[FOOD] == round(food_d5 - 20, 1)

    c2 = arm_crisis(eng, "mutiny")
    gs.day = 6
    c2["day"] = 6
    food_d6 = gs.resources[FOOD]
    # 同事件同选项、不同 token 不同天：必须是一次全新结算而非回放
    detail, replayed = eng.resolve_crisis("mutiny", "double_ration", token=c2["token"])
    assert replayed is False
    assert gs.resources[FOOD] == round(food_d6 - 20, 1)
    assert gs.last_resolution["day"] == 6


def test_stale_token_rejected(db):
    """token 与当前待处理危机不符（过期/串档请求）：拒绝结算。"""
    from app.services.engine import BunkerEngineConflict

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_crisis("mutiny", "suppress", token="stale-token")
    assert gs.pending_crisis is not None
    assert gs.pending_crisis["token"] == crisis["token"]


def test_concurrent_resolve_only_one_wins(db):
    """两个独立会话并发结算同一危机：乐观锁保证效果只落一次。"""
    from sqlalchemy.orm.exc import StaleDataError

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    db.commit()
    sid, token = gs.id, crisis["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        ea = BunkerEngine(db_a, ga, rand=FixedRand())
        eb = BunkerEngine(db_b, gb, rand=FixedRand())
        ea.resolve_crisis("mutiny", "double_ration", token=token)
        db_a.commit()
        # B 持有的版本号已过期：提交时 StaleDataError，食物不会被再扣一次
        eb.resolve_crisis("mutiny", "double_ration", token=token)
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()

        final = db_a.get(GameSession, sid)
        assert final.resources[FOOD] == round(300 - 20, 1)
        assert final.pending_crisis is None
    finally:
        db_a.close()
        db_b.close()


def test_operations_locked_during_crisis_phase(db):
    """危机阶段统一拒绝建造/升级/调岗，状态机只有 daily/crisis/ended。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.advance_day()
    fid = gs.facilities[0].id
    rid = gs.residents[0].id
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")


def test_triggered_crisis_matches_pool_and_phase(db):
    """推进触发危机后进入 crisis 阶段，待处理事件来自危机池且带 token。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    assert eng.phase == "crisis"
    assert crisis["event"] == CRISIS_POOL[0]["key"]
    assert crisis["token"]
    # 完成结算后回到每日阶段
    all_choice = next(c["key"] for c in crisis["choices"] if not c["targeted"])
    eng.resolve_crisis(crisis["event"], all_choice, token=crisis["token"])
    assert eng.phase == "daily"


def test_reaching_target_day_ends_without_pending_crisis(db):
    """终局优先：抵达目标日直接胜利，不挂起无法处理的危机。"""
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng = BunkerEngine(db, gs, rand=TriggerRand())  # 即便必定触发危机
    crisis = eng.advance_day()
    assert crisis is None
    assert gs.status == "win"
    assert gs.pending_crisis is None
    assert eng.phase == "ended"


def test_old_archive_migration_backfills_columns(db):
    """旧结构表（无新列）经 ensure_schema 后可正常读写，旧档案停在每日阶段。"""
    from sqlalchemy import text
    from app.core.migration import ensure_schema as ensure_schema_migration
    from app.core import database as db_module

    gs = make_session(db)
    db.commit()
    # expire_all：避免 ORM 中被标脏的 GameSession 在 DDL 后 autoflush 到空表，
    # 保证下面的“旧表数据搬运”基于已提交的真实存量行
    db.expire_all()
    # 模拟旧版表结构：移除新增列（SQLite 走重建表）
    db.execute(text("ALTER TABLE game_sessions RENAME TO game_sessions_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM game_sessions_old"
    ))
    db.execute(text("DROP TABLE game_sessions_old"))
    db.commit()

    ensure_schema_migration(db_module.engine)
    db.expire_all()
    gs = db.query(GameSession).first()
    assert gs.pending_crisis is None
    assert gs.last_resolution is None
    assert gs.row_version == 1
    # 旧档案处于每日阶段，可正常推进
    eng = BunkerEngine(db, gs, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()
    db.commit()