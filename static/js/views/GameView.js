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
      if (this.s.status !== "running" || this.crisis) return;
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
      if (this.crisis) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/build`, { category: cat });
      } catch (e) { this.error = e.message; }
    },
    async upgrade(fid) {
      this.error = "";
      if (this.crisis) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/upgrade/${fid}`);
      } catch (e) { this.error = e.message; }
    },
    async assignJob(rid, job) {
      this.error = "";
      if (this.crisis) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/resident/${rid}/job`, { job });
      } catch (e) { this.error = e.message; }
    },
    setJobSel(rid, job) { this.selectedJob[rid] = job; },
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
      <button class="btn primary advance" :disabled="loading || s.status!=='running' || !!crisis" :title="crisis ? '请先处理当前危机' : ''" @click="advance">
        {{ crisis ? '等待危机抉择' : loading ? '推进中…' : '推进一天' }}
      </button>
    </section>
    <div v-if="error" class="msg err global">{{ error }}</div>

    <!-- 主区 -->
    <div class="game-body">
      <nav class="tabs">
        <button :class="{ active: tab==='overview' }" @click="tab='overview'">总览</button>
        <button :class="{ active: tab==='residents' }" @click="tab='residents'">幸存者 ({{ alive.length }})</button>
        <button :class="{ active: tab==='build' }" @click="tab='build'">设施扩建</button>
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
            <button v-if="s.status==='running'" class="btn tiny" :disabled="!!crisis" @click="upgrade(f.id)">升级</button>
          </div>
        </div>
      </div>

      <!-- 幸存者 -->
      <div v-if="tab==='residents'">
        <div v-for="r in s.residents" :key="r.id" class="person" :class="{ dead: !r.alive }">
          <div class="p-avatar">{{ r.name[0] }}</div>
          <div class="p-info">
            <div class="p-name">{{ r.name }} <span class="dim">{{ r.job_zh }}</span></div>
            <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: r.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(r.health) }}</b></div>
            <div class="meter"><i>士气</i><span class="track"><span class="fill" :style="{width: r.morale+'%', background:'#ffb300'}"></span></span><b>{{ fmt(r.morale) }}</b></div>
          </div>
          <div class="p-actions" v-if="r.alive && s.status==='running'">
            <select :value="r.job" :disabled="!!crisis" @change="assignJob(r.id, $event.target.value)">
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
            <button class="btn small primary" :disabled="s.status!=='running' || !!crisis" @click="build(b.category)">建造</button>
          </div>
        </div>
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