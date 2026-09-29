# -*- coding: utf-8 -*-
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class SessionCreate(BaseModel):
    name: str = "末日地堡档案"


class SessionBrief(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    survivors: int
    score: int

    class Config:
        from_attributes = True


class ResidentOut(BaseModel):
    id: int
    name: str
    job: str
    job_zh: Optional[str] = None
    health: float
    morale: float
    alive: int
    joined_day: int

    class Config:
        from_attributes = True


class FacilityOut(BaseModel):
    id: int
    name: str
    category: str
    level: int
    status: str
    built_day: int

    class Config:
        from_attributes = True


class LogOut(BaseModel):
    id: int
    day: int
    event_type: str
    title: str
    detail: str
    decision: Optional[str] = None

    class Config:
        from_attributes = True


class SessionDetail(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    resources: Dict[str, float]
    survivors: int
    score: int
    outcome: Optional[Dict[str, Any]] = None
    # 待处理危机快照：刷新/重进档案后前端据此恢复决策弹层
    pending_crisis: Optional[Dict[str, Any]] = None
    residents: List[ResidentOut] = []
    facilities: List[FacilityOut] = []
    logs: List[LogOut] = []


class AdvanceResult(BaseModel):
    session: SessionDetail
    crisis: Optional[Dict[str, Any]] = None


class CrisisChoice(BaseModel):
    event_key: str
    choice_key: str
    target_id: Optional[int] = None
    # 待处理危机的一次性凭据，用于识别过期/并发的旧请求；旧客户端可省略
    token: Optional[str] = None


class JobAssign(BaseModel):
    job: str


class BuildRequest(BaseModel):
    category: str


class BuildableInfo(BaseModel):
    category: str
    name: str
    cost: Dict[str, float]
    level_scale: float


class EngineConfig(BaseModel):
    resources: Dict[str, float]
    facility_costs: Dict[int, Dict[str, float]]
    facility_names: Dict[str, str]
    job_options: List[str]
    status: str


class Message(BaseModel):
    detail: str = "ok"