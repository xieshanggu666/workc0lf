/* 末日地堡生存 —— 主游戏界面 */
window.GameView = {
  props: ["sid", "onExit"],
  data() {
    return {
      s: null,
      crisis: null,
      loading: false,
      error: "",
      tab: "overview",
      config: null,
      buildings: [],
      selectedJob: {},
      expParty: {},
      expSupplies: { food: 0, water: 0, power: 0, oxygen: 0 },
      expBusy: false,
      expMessage: "",
      expResult: "",
    };
  },
  created() { this.init(); },
  methods: {
    async init() {
      this.error = "";
      try {
        const [s, cfg, bld] = await Promise.all([
          Api.get(`/api/sessions/${this.sid}`),
          Api.get("/api/config"),
          Api.get("/api/buildings"),
        ]);
        this.s = s; this.config = cfg; this.buildings = bld;
        // 待处理危机已随存档持久化：刷新/重进档案后恢复同一个决策弹层
        this.crisis = s.pending_crisis || null;
      } catch (e) { this.error = e.message; }
    },
    async loadSession() {
      this.s = await Api.get(`/api/sessions/${this.sid}`);
      // 以服务端为准恢复待处理危机（并发落败回放时也可能带回）
      this.crisis = this.s.pending_crisis || null;
    },
    async advance() {
      this.error = "";
      if (this.s.status !== "running" || this.crisis || this.expeditionEncounter) return;
      this.loading = true;
      try {
        const r = await Api.post(`/api/sessions/${this.sid}/advance`);
        this.s = r.session;
        this.crisis = r.crisis || null;
      } catch (e) {
        this.error = e.message;
        // 并发落败等 409 场景：拉取最新状态，避免覆盖掉已挂起的危机
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async resolve(c) {
      this.error = "";
      this.loading = true;
      try {
        // 目标语义以后端下发的 c.targeted 为准：
        // 仅单体决策回传 target_id；全体决策显式传 null，
        // 避免危机事件的随机目标被无条件带回、把全体效果收窄成一人。
        // token 绑定本次待处理危机：重复/并发请求由后端识别为同一次结算
        const body = {
          event_key: this.crisis.event,
          choice_key: c.key,
          target_id: c.targeted ? this.crisis.target_id : null,
          token: this.crisis.token,
        };
        this.s = await Api.post(`/api/sessions/${this.sid}/resolve`, body);
        this.crisis = this.s.pending_crisis || null;
      } catch (e) {
        this.error = e.message;
        // 409（过期/并发）或危机已被其他标签页结算：刷新为最新状态
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async build(cat) {
      this.error = "";
      if (this.crisis || this.expeditionEncounter) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/build`, { category: cat });
      } catch (e) { this.error = e.message; }
    },
    async upgrade(fid) {
      this.error = "";
      if (this.crisis || this.expeditionEncounter) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/upgrade/${fid}`);
      } catch (e) { this.error = e.message; }
    },
    async assignJob(rid, job) {
      this.error = "";
      if (this.crisis || this.expeditionEncounter) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/resident/${rid}/job`, { job });
      } catch (e) { this.error = e.message; }
    },
    setJobSel(rid, job) { this.selectedJob[rid] = job; },
    token() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return `t-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    },
    toggleExplorer(rid) {
      if (this.expedition) return;
      this.expParty[rid] = !this.expParty[rid];
    },
    updateSupply(k, e) {
      const n = Number(e.target.value || 0);
      this.expSupplies[k] = Math.max(0, Math.min(n, this.s.resources[k] || 0));
    },
    async startExpedition() {
      this.error = "";
      this.expMessage = "";
      const ids = this.availableResidents.filter(r => this.expParty[r.id]).map(r => r.id);
      if (!ids.length) {
        this.expMessage = "请先选择至少一名幸存者";
        return;
      }
      this.expBusy = true;
      try {
        const provisions = {};
        for (const k of ["food", "water", "power", "oxygen"]) {
          if (Number(this.expSupplies[k] || 0) > 0) provisions[k] = Number(this.expSupplies[k]);
        }
        const body = { resident_ids: ids, provisions, token: this.token() };
        const r = await Api.post(`/api/sessions/${this.sid}/expeditions`, body);
        this.s = r.session;
        this.tab = "expedition";
        this.expParty = {};
        this.expSupplies = { food: 0, water: 0, power: 0, oxygen: 0 };
      } catch (e) {
        this.expMessage = e.message;
        await this.loadSession();
      } finally {
        this.expBusy = false;
      }
    },
    async expeditionAction(action) {
      this.error = "";
      if (!this.expeditionEncounter) return;
      this.expBusy = true;
      try {
        const body = { action_key: action.key, token: this.expeditionEncounter.token };
        const r = await Api.post(`/api/sessions/${this.sid}/expeditions/encounter`, body);
        this.s = r.session;
        this.expResult = r.detail;
      } catch (e) {
        this.error = e.message;
        await this.loadSession();
      } finally {
        this.expBusy = false;
      }
    },
    expMember(rid) {
      return this.s?.active_expedition?.party?.find(p => p.id === rid);
    },
    resPct(k) {
      const cap = { food: 300, water: 300, power: 200, oxygen: 200 };
      const c = cap[k] || 100;
      return Math.min(100, Math.round((this.s.resources[k] / c) * 100));
    },
    clazz(st) {
      return st === "win" ? "win" : st === "over" ? "over" : "running";
    },
    fmt(v) { return v == null ? "-" : Math.round(v); },
  },
  computed: {
    alive() { return this.s ? this.s.residents.filter(r => r.alive) : []; },
    availableResidents() { return this.alive.filter(r => !r.away); },
    expedition() { return this.s?.active_expedition || null; },
    expeditionEncounter() { return this.expedition?.pending_encounter || null; },
    expeditionParty() {
      if (!this.expedition) return [];
      return this.expedition.party.map(p => ({
        ...p,
        job_zh: this.s.residents.find(r => r.id === p.id)?.job_zh || "",
      }));
    },
    expeditionLootText() {
      if (!this.expedition) return "";
      const map = { food: "食物", water: "水源", power: "电力", oxygen: "氧气" };
      return Object.entries(this.expedition.loot || {})
        .filter(([, v]) => v > 0).map(([k, v]) => `${map[k]} ${Math.round(v)}`).join("、") || "尚无战利品";
    },
  },
  template: `
  <div v-if="s" class="game" :class="clazz(s.status)">
    <!-- 顶栏 -->
    <header class="game-top">
      <div class="brand">末日地堡<i class="bar"></i></div>
      <div class="day">{{ s.day }}<small>/{{ s.target_day }} 天</small></div>
      <div class="top-right">
        <span class="chip" :class="s.status">{{ s.status === 'running' ? '进行中' : s.status === 'win' ? '胜利' : '失败' }}</span>
        <button class="btn ghost small" @click="onExit">返回档案</button>
      </div>
    </header>

    <!-- 资源条 -->
    <section class="resbar">
      <div v-for="k in ['food','water','power','oxygen']" :key="k" class="res" :class="{ low: s.resources[k] < 20 && s.status==='running' }">
        <div class="res-name">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}</div>
        <div class="res-val">{{ fmt(s.resources[k]) }}</div>
        <div class="res-track"><div class="res-fill" :class="k" :style="{ width: resPct(k)+'%' }"></div></div>
      </div>
      <button class="btn primary advance" :disabled="loading || s.status!=='running' || !!crisis || !!expeditionEncounter" :title="crisis ? '请先处理当前危机' : expeditionEncounter ? '请先处理探索遭遇' : ''" @click="advance">
        {{ crisis ? '等待危机抉择' : expeditionEncounter ? '等待探索抉择' : expedition && expedition.returning ? '推进一天（归队）' : loading ? '推进中…' : '推进一天' }}
      </button>
    </section>
    <div v-if="error" class="msg err global">{{ error }}</div>

    <!-- 主区 -->
    <div class="game-body">
      <nav class="tabs">
        <button :class="{ active: tab==='overview' }" @click="tab='overview'">总览</button>
        <button :class="{ active: tab==='residents' }" @click="tab='residents'">幸存者 ({{ alive.length }})</button>
        <button :class="{ active: tab==='build' }" @click="tab='build'">设施扩建</button>
        <button :class="{ active: tab==='expedition' }" @click="tab='expedition'">
          外出探索<span v-if="expedition" class="tab-dot">！</span>
        </button>
        <button :class="{ active: tab==='log' }" @click="tab='log'">大事记</button>
      </nav>

      <!-- 总览 -->
      <div v-if="tab==='overview'">
        <div class="cards">
          <div class="card"><div class="k">幸存者</div><div class="v">{{ s.survivors }}</div><div class="hint">人口即火种</div></div>
          <div class="card"><div class="k">士气</div><div class="v">{{ s.residents.length ? fmt(alive.reduce((a,r)=>a+r.morale,0)/alive.length) : 0 }}</div><div class="hint">影响产出效率</div></div>
          <div class="card"><div class="k">设施</div><div class="v">{{ s.facilities.length }}</div><div class="hint">支撑循环</div></div>
          <div class="card"><div class="k">得分</div><div class="v">{{ s.score }}</div><div class="hint">生存评分</div></div>
        </div>
        <div class="fac-grid">
          <div v-for="f in s.facilities" :key="f.id" class="fac">
            <span class="fac-name">{{ f.name }}</span>
            <span class="chip">Lv.{{ f.level }}</span>
            <span class="dim">{{ {farm:'产食物',water:'产水源',power:'发电',oxygen:'产氧',med:'医疗',storage:'仓储'}[f.category] }}</span>
            <button v-if="s.status==='running'" class="btn tiny" :disabled="!!crisis || !!expeditionEncounter" @click="upgrade(f.id)">升级</button>
          </div>
        </div>
      </div>

      <!-- 幸存者 -->
      <div v-if="tab==='residents'">
        <div v-for="r in s.residents" :key="r.id" class="person" :class="{ dead: !r.alive, away: r.alive && r.away }">
          <div class="p-avatar">{{ r.name[0] }}</div>
          <div class="p-info">
            <div class="p-name">{{ r.name }} <span class="dim">{{ r.job_zh }}</span><span v-if="r.away" class="chip away-chip">外出中</span></div>
            <template v-if="r.away && expMember(r.id)">
              <div class="meter"><i>状态</i><span class="track"><span class="fill" :style="{width: expMember(r.id).health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(expMember(r.id).health) }}</b></div>
              <div class="dim">外出健康/士气在归队时统一写回；地堡岗位与生产已暂停</div>
            </template>
            <template v-else>
              <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: r.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(r.health) }}</b></div>
              <div class="meter"><i>士气</i><span class="track"><span class="fill" :style="{width: r.morale+'%', background:'#ffb300'}"></span></span><b>{{ fmt(r.morale) }}</b></div>
            </template>
          </div>
          <div class="p-actions" v-if="r.alive && s.status==='running'">
            <select :value="r.job" :disabled="r.away || !!crisis || !!expeditionEncounter" @change="assignJob(r.id, $event.target.value)">
              <option value="engineer">工程师</option>
              <option value="farmer">农民</option>
              <option value="general">杂工</option>
            </select>
          </div>
        </div>
      </div>

      <!-- 扩建 -->
      <div v-if="tab==='build'">
        <div class="build-grid">
          <div v-for="b in buildings" :key="b.category" class="build-card">
            <span class="bc-name">{{ b.name }}</span>
            <span class="dim">等级加成 x1.6</span>
            <div class="cost" v-for="(v,k) in b.cost" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</div>
            <button class="btn small primary" :disabled="s.status!=='running' || !!crisis || !!expeditionEncounter" @click="build(b.category)">建造</button>
          </div>
        </div>
      </div>

      <!-- 外出探索 -->
      <div v-if="tab==='expedition'" class="expedition-panel">
        <template v-if="!expedition">
          <div class="exp-head">
            <div>
              <h3>组建探索队</h3>
              <p class="dim">选择离堡居民与额外携带物资。每名队员至少携带 2 食物、1 水；离堡期间暂停岗位生产，遭遇在途中处理，伤亡和战利品归队时一次结算。</p>
            </div>
          </div>
          <div class="exp-picker">
            <label v-for="r in availableResidents" :key="r.id" class="exp-person" :class="{ picked: expParty[r.id] }">
              <input type="checkbox" :checked="!!expParty[r.id]" @change="toggleExplorer(r.id)">
              <strong>{{ r.name }}</strong><span class="dim">{{ r.job_zh }}</span>
            </label>
            <div v-if="!availableResidents.length" class="dim">堡内暂无可派出的幸存者</div>
          </div>
          <div class="supply-picker">
            <label v-for="k in ['food','water','power','oxygen']" :key="k">
              <span>{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}</span>
              <input class="input" type="number" min="0" step="0.1" :max="s.resources[k]" :value="expSupplies[k]" @input="updateSupply(k, $event)">
            </label>
          </div>
          <div v-if="expMessage" class="msg err">{{ expMessage }}</div>
          <button class="btn primary" :disabled="s.status!=='running' || !!crisis || !!expeditionEncounter || expBusy" @click="startExpedition">
            {{ expBusy ? '出发中…' : '携带物资离堡' }}
          </button>
        </template>

        <template v-else>
          <div class="exp-status">
            <div>
              <h3>探索队在外 <span class="chip running">{{ expedition.depth }}/{{ 3 }} 区域</span></h3>
              <p class="dim">
                第{{ expedition.started_day }}天离堡 ·
                <b>{{ expedition.returning ? '返程中，推进一天后归队结算' : '等待探索遭遇抉择' }}</b>
              </p>
            </div>
            <div class="loot-box">{{ expeditionLootText }}</div>
          </div>
          <div class="exp-party-grid">
            <div v-for="p in expeditionParty" :key="p.id" class="exp-card" :class="{ dead: !p.alive }">
              <strong>{{ p.name }}</strong>
              <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: p.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(p.health) }}</b></div>
              <div class="meter"><i>士气</i><span class="track"><span class="fill" :style="{width: p.morale+'%', background:'#ffb300'}"></span></span><b>{{ fmt(p.morale) }}</b></div>
            </div>
          </div>
          <div v-if="expResult" class="exp-result">{{ expResult }}</div>
          <div class="exp-log">
            <div v-for="(line,i) in expedition.log" :key="i" class="exp-log-line">{{ line }}</div>
          </div>
        </template>
      </div>

      <!-- 大事记 -->
      <div v-if="tab==='log'" class="logs">
        <div v-for="l in [...s.logs].reverse()" :key="l.id" class="log" :class="l.event_type">
          <span class="log-day">D{{ l.day }}</span>
          <div class="log-txt"><strong>{{ l.title }}</strong><p>{{ l.detail }}</p></div>
        </div>
      </div>
    </div>

    <!-- 结局弹层 -->
    <div v-if="s.status !== 'running'" class="overlay">
      <div class="ending" :class="s.status">
        <h2>{{ s.status === 'win' ? '曙光降临' : '地堡永寂' }}</h2>
        <p>{{ s.outcome.reason }}</p>
        <div class="end-stats">
          <div><span>存活天数</span><b>{{ s.outcome.day }}</b></div>
          <div><span>幸存者</span><b>{{ s.outcome.survivors }}</b></div>
          <div><span>得分</span><b>{{ s.score }}</b></div>
        </div>
        <button class="btn primary" @click="onExit">返回档案列表</button>
      </div>
    </div>

    <!-- 探索遭遇弹层 -->
    <div v-if="expeditionEncounter" class="overlay">
      <div class="crisis expedition">
        <h2>🧭 {{ expeditionEncounter.title }}</h2>
        <p class="crisis-desc">{{ expeditionEncounter.desc }}</p>
        <div class="crisis-tgt">
          当前区域 {{ expeditionEncounter.depth }}/{{ 3 }} · 相关队员：{{ expeditionEncounter.target_name }}
        </div>
        <div class="choices">
          <button v-for="a in expeditionEncounter.choices" :key="a.key" class="choice" :disabled="expBusy" @click="expeditionAction(a)">
            <strong>{{ a.label }}</strong>
            <span class="scope-tag">{{ a.key === 'turn_back' ? '返程' : '行动' }}</span>
            <span class="hint">{{ a.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 危机弹层 -->
    <div v-if="crisis" class="overlay">
      <div class="crisis">
        <h2>⚡ {{ crisis.title }}</h2>
        <p class="crisis-desc">{{ crisis.desc }}</p>
        <div v-if="crisis.needs_target" class="crisis-tgt">
          相关居民：{{ crisis.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in crisis.choices" :key="c.key" class="choice" @click="resolve(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全体' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>`,
};