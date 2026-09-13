/* ============================================================
   微信账号配置中心 —— 后台页面组件（可直接贴进 static/admin-v2.html）
   ------------------------------------------------------------
   上线三步：
     1) 把本文件内容贴进 static/admin-v2.html（放在其它 Vue.component 定义一起）
     2) 菜单：在 menuGroupsData 的 sys-mgr 组里加一项 WX_ACCOUNTS_MENU_ITEM
              （就在 {key:'payment-channels',label:'支付渠道'} 后面）
     3) 挂载：在 #app 模板里加一行
              <page-wx-accounts v-if="$parent.activeMenu==='wx-accounts'"></page-wx-accounts>
   依赖：宿主页面的 $parent.api(path, data, method) 与 $parent.toast(msg, type)
        （admin-v2.html 里本来就有；没有时会自动退化成原生 fetch）
   ============================================================ */

var WX_ACCOUNTS_MENU_ITEM = { key: 'wx-accounts', label: '微信账号' };

Vue.component('page-wx-accounts', {
  template: `
<div class="page-box">
  <div class="box-header">
    <h3>微信账号<span class="wx-sub">小程序 / 公众号 一键切换 · 连续失败自动降级 · 改完立刻生效</span></h3>
    <div>
      <button class="btn btn-sm btn-blue" @click="load(true)">刷新</button>
      <button class="btn btn-sm btn-blue" @click="toggleReveal">{{reveal?'隐藏密钥':'显示密钥'}}</button>
      <button class="btn btn-sm btn-blue" @click="openAdd(tab)">+ 添加{{tab==='mp'?'小程序':'公众号'}}</button>
    </div>
  </div>

  <div class="wx-eff-wrap">
    <div class="wx-eff" :class="{on: effOf(t.k) && effOf(t.k).source==='db'}" v-for="t in TYPES" :key="t.k">
      <div class="t">{{t.label}}
        <span class="wx-tag" :class="(effOf(t.k) && effOf(t.k).source==='db')?'on':'warn'">
          {{(effOf(t.k) && effOf(t.k).source==='db')?'数据库生效':'config.py 兜底'}}
        </span>
      </div>
      <div class="wx-kv"><span class="k">名称</span><span class="v">{{nameOf(t.k)}}</span></div>
      <div class="wx-kv"><span class="k">appid</span><span class="v">{{appidOf(t.k)}}</span></div>
      <div class="wx-kv"><span class="k">secret</span><span class="v">{{secretOf(t.k)}}</span></div>
      <div class="wx-kv"><span class="k">主体</span><span class="v">{{subjOf(t.k)}}</span></div>
      <div class="wx-kv"><span class="k">商户号关联</span><span class="v">{{mchText(effOf(t.k))}}</span></div>
    </div>
  </div>

  <div class="wx-tip">
    切换：点某行的「<b>设为生效</b>」→ 立刻生效（业务实时读库，<b>不用重启、不用发版</b>）。<br>
    自动降级：生效账号连续 <b>{{failThreshold}}</b> 次调用失败 → 自动停用并切到备用账号，日志留痕。<br>
    ⚠️ 切到新小程序前，务必先办好：新小程序<b>代码已上传</b>、<b>服务器域名白名单</b>、<b>微信支付关联 appid</b>、<b>订阅消息模板申请</b>；否则切过去用户打不开。
  </div>

  <div class="wx-tabs">
    <div class="t" :class="{active:tab==='mp'}" @click="tab='mp'">小程序池 ({{count('mp')}})</div>
    <div class="t" :class="{active:tab==='oa'}" @click="tab='oa'">公众号池 ({{count('oa')}})</div>
  </div>

  <table>
    <thead><tr>
      <th>ID</th><th>名称</th><th>appid</th><th>识别前缀</th><th>主体</th><th>优先级</th><th>状态</th>
      <th>健康</th><th>失败</th><th>最后使用</th><th class="wx-nowrap">操作</th>
    </tr></thead>
    <tbody>
      <tr v-for="a in accounts" :key="a.id" :style="a.is_active?'background:#f6fdf9':''">
        <td>{{a.id}}</td>
        <td>{{a.name}}</td>
        <td class="wx-mono">{{a.appid}}<span v-if="a.usable===false" class="wx-tag off" :title="a.usable_reason" style="margin-left:4px">未配好</span></td>
        <td class="wx-mono" title="判断某个 openid 是不是这个号下面的（换号后要改这里）">{{a.openid_prefix||'-'}}</td>
        <td>{{a.subject||'-'}}</td>
        <td>{{a.priority}}</td>
        <td>
          <span class="wx-tag" :class="a.auto_disabled?'auto':(a.is_active?'on':'off')">
            {{a.auto_disabled?'被自动停用':(a.is_active?'当前生效':'备用待命')}}
          </span>
        </td>
        <td><span class="wx-tag" :class="a.health_status||'unknown'">{{a.health_status||'unknown'}}</span></td>
        <td>{{a.fail_count}}</td>
        <td>{{a.last_used_at||'-'}}</td>
        <td class="wx-nowrap">
          <button class="btn-text green" v-if="!a.is_active" :disabled="a.usable===false" :title="a.usable_reason" @click="switchAccount(a)">设为生效</button>
          <button class="btn-text orange" v-if="a.is_active" @click="toggleAccount(a,false)">停用</button>
          <button class="btn-text blue" v-if="!a.is_active" :disabled="a.usable===false" :title="a.usable_reason" @click="toggleAccount(a,true)">启用</button>
          <button class="btn-text blue" :disabled="a.usable===false" :title="a.usable_reason" @click="probe(a)">探活</button>
          <button class="btn-text" @click="openEdit(a)">编辑</button>
          <button class="btn-text red" @click="removeAccount(a)">删除</button>
        </td>
      </tr>
      <tr v-if="!accounts.length"><td colspan="11" class="empty-row">暂无账号，点右上角添加</td></tr>
    </tbody>
  </table>

  <div class="wx-section-title">可切换配置项（域名 / 回调 / 入口）</div>
  <table>
    <thead><tr><th>配置项</th><th>当前值</th><th>来源</th><th>说明</th><th></th></tr></thead>
    <tbody>
      <tr v-for="c in configItems" :key="c.cfg_key">
        <td class="wx-mono">{{c.cfg_key}}</td>
        <td style="max-width:340px"><input class="wx-cfg-input" v-model="c.cfg_value"></td>
        <td><span class="wx-tag" :class="c.from_db?'on':'off'">{{c.from_db?'数据库':'默认'}}</span></td>
        <td style="white-space:normal;max-width:380px;color:#909399;font-size:12.5px">{{c.note}}</td>
        <td><button class="btn-text blue" @click="saveCfg(c)">保存</button></td>
      </tr>
    </tbody>
  </table>

  <div class="wx-section-title">消息模板（小程序订阅通知 / 公众号模板消息，两条路各自独立）</div>
  <table>
    <thead><tr><th>业务</th><th>通道</th><th>模板ID</th><th>落地页</th><th>说明</th></tr></thead>
    <tbody>
      <tr v-for="t in templates" :key="t.id">
        <td>{{t.biz_label}}<div class="wx-mono" style="color:#909399">{{t.biz}}</div></td>
        <td><span class="wx-tag" :class="t.channel==='mp'?'on':'warn'">{{t.channel==='mp'?'小程序订阅':'公众号模板'}}</span></td>
        <td class="wx-mono" style="max-width:300px">{{t.template_id}}</td>
        <td class="wx-mono" style="color:#909399">{{t.page||'-'}}</td>
        <td style="white-space:normal;max-width:340px;color:#909399;font-size:12.5px">{{t.note}}</td>
      </tr>
      <tr v-if="!templates.length"><td colspan="5" class="empty-row">暂无模板</td></tr>
    </tbody>
  </table>

  <div class="wx-section-title">切换日志（手动切换 + 自动降级都留痕）</div>
  <table>
    <thead><tr><th>时间</th><th>类型</th><th>从</th><th>到</th><th>原因</th><th>操作人</th></tr></thead>
    <tbody>
      <tr v-for="l in log" :key="l.id">
        <td class="wx-mono">{{l.created_at}}</td>
        <td>{{l.acct_type==='mp'?'小程序':'公众号'}}</td>
        <td>{{l.from_name||'-'}}</td>
        <td>{{l.to_name||'-'}}</td>
        <td style="white-space:normal;max-width:420px">{{l.reason}}</td>
        <td>{{l.operator}}</td>
      </tr>
      <tr v-if="!log.length"><td colspan="6" class="empty-row">还没有切换记录</td></tr>
    </tbody>
  </table>

  <div class="modal-mask" v-if="modal" @click.self="closeModal">
    <div class="modal-box md">
      <div class="modal-header"><h3>{{formTitle}}</h3><span class="close" @click="closeModal">×</span></div>
      <div class="modal-body">
        <div class="form-row">
          <div class="form-group"><label>类型</label>
            <select v-model="form.acct_type"><option value="mp">小程序</option><option value="oa">公众号</option></select>
          </div>
          <div class="form-group"><label>名称（给你自己看的）</label><input v-model="form.name" placeholder="例：备用小程序-异主体"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>AppID</label><input v-model="form.appid" placeholder="wx..."></div>
          <div class="form-group"><label>AppSecret</label><input v-model="form.secret" placeholder="换号必填"></div>
          <div class="form-group"><label>openid 识别前缀（换号必填）</label><input v-model="form.openid_prefix" placeholder="例：ooTcRx / oLhbm2 —— 新号第一次授权后，看日志或库里的 openid 前 6 位"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>主体（决定 unionid 是否共享）</label><input v-model="form.subject" placeholder="例：重庆科莱维科技有限公司"></div>
          <div class="form-group"><label>支付商户号关联 appid</label>
            <select v-model="form.mch_relation">
              <option value="none">未关联</option><option value="pending">申请中</option><option value="ok">已关联</option>
            </select>
            <div class="form-hint">没关联的话，切过去支付会直接报错</div>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>优先级（小的优先，备用号排后面）</label><input type="number" v-model.number="form.priority"></div>
          <div class="form-group"><label>Token（服务器配置用，可留空）</label><input v-model="form.token"></div>
        </div>
        <div class="form-group"><label>备注</label><textarea v-model="form.note" rows="2" placeholder="例：异主体公司注册，已绑同一开放平台账号"></textarea></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm" @click="closeModal">取消</button>
        <button class="btn btn-sm btn-blue" :disabled="saving" @click="saveAccount">{{saving?'保存中…':'保存'}}</button>
      </div>
    </div>
  </div>
</div>`,
  data: function () {
    return {
      TYPES: [{ k: 'mp', label: '小程序' }, { k: 'oa', label: '公众号' }],
      tab: 'mp',
      reveal: false,
      snap: null,
      modal: false,
      form: {},
      formTitle: '添加账号',
      saving: false,
      failThreshold: 3
    };
  },
  computed: {
    accounts: function () {
      var t = this.tab, list = (this.snap && this.snap.accounts) || [];
      return list.filter(function (a) { return a.acct_type === t; });
    },
    configItems: function () { return (this.snap && this.snap.config) || []; },
    templates: function () { return (this.snap && this.snap.templates) || []; },
    log: function () { return (this.snap && this.snap.log) || []; }
  },
  created: function () { this.load(true); },
  methods: {
    /* ---------- 基础设施：优先用宿主后台的 api()，保证鉴权头一致 ---------- */
    _api: function (path, data, method) {
      var p = this.$parent, app = this.$root;
      if (p && typeof p.api === 'function') { return p.api(path, data, method); }
      if (app && typeof app.api === 'function') { return app.api(path, data, method); }
      var m = method || (data ? 'POST' : 'GET');
      var url = '/api' + path;
      if (m === 'GET' && data) {
        var q = [];
        Object.keys(data).forEach(function (k) { if (data[k] !== undefined && data[k] !== null) q.push(k + '=' + encodeURIComponent(data[k])); });
        if (q.length) { url += '?' + q.join('&'); data = null; }
      }
      var opts = { method: m, headers: { 'Content-Type': 'application/json' } };
      if (data) { opts.body = JSON.stringify(data); }
      return fetch(url, opts).then(function (r) { return r.json(); }).then(function (d) {
        if (d.code && d.code !== 0 && d.code !== 200) { throw new Error(d.message || '请求失败'); }
        return d;
      });
    },
    _toast: function (msg, type) {
      var p = this.$parent;
      if (p && typeof p.toast === 'function') { p.toast(msg, type); }
      else { console.log('[toast]', type || 'success', msg); }
    },
    _confirm: function (msg, cb) {
      var p = this.$parent;
      if (p && typeof p.confirm2 === 'function') { p.confirm2(msg, cb); }
      else if (window.confirm(msg)) { cb(); }
    },
    _fail: function (e) { this._toast((e && e.message) || '操作失败', 'error'); },

    /* ---------- 数据 ---------- */
    load: function (withSpinner) {
      var self = this;
      return this._api('/wx-config/snapshot' + (this.reveal ? '?reveal=1' : ''), null, 'GET').then(function (d) {
        self.snap = d.data;
        self.failThreshold = (d.data && d.data.fail_threshold) || 3;
      }).catch(function (e) { self._fail(e); });
    },
    toggleReveal: function () { this.reveal = !this.reveal; this.load(); },
    count: function (t) {
      var list = (this.snap && this.snap.accounts) || [];
      return list.filter(function (a) { return a.acct_type === t; }).length;
    },
    effOf: function (t) { return (this.snap && this.snap.effective && this.snap.effective[t]) || {}; },
    nameOf: function (t) { return this.effOf(t).name || '-'; },
    appidOf: function (t) { return this.effOf(t).appid || '-'; },
    subjOf: function (t) { return this.effOf(t).subject || '-'; },
    secretOf: function (t) {
      var c = this.effOf(t), s = c.secret || '';
      if (!s) { return '-'; }
      return this.reveal ? s : (s.slice(0, 6) + '…****');
    },
    mchText: function (c) {
      var v = (c && c.mch_relation) || 'none';
      return { none: '未关联（切过去支付会报错）', pending: '申请中', ok: '已关联' }[v] || v;
    },

    /* ---------- 账号操作 ---------- */
    openAdd: function (type) {
      this.form = { acct_type: type || 'mp', name: '', appid: '', secret: '', subject: '',
                    mch_relation: 'pending', priority: 100, token: '', note: '', openid_prefix: '' };
      this.formTitle = '添加' + (this.form.acct_type === 'mp' ? '小程序' : '公众号');
      this.modal = true;
    },
    openEdit: function (a) {
      this.form = Object.assign({}, a);
      this.formTitle = '编辑：' + a.name;
      this.modal = true;
    },
    closeModal: function () { this.modal = false; },
    saveAccount: function () {
      var self = this, f = this.form, isNew = !f.id;
      this.saving = true;
      var body = {
        acct_type: f.acct_type, name: f.name, appid: f.appid, secret: f.secret,
        subject: f.subject, mch_relation: f.mch_relation, priority: f.priority,
        token: f.token, note: f.note, openid_prefix: f.openid_prefix
      };
      var p = isNew ? this._api('/wx-config/accounts', body, 'POST')
                    : this._api('/wx-config/accounts/' + f.id, body, 'POST');
      p.then(function () {
        self.saving = false; self.modal = false;
        self._toast(isNew ? '已添加' : '已保存');
        self.load();
      }).catch(function (e) { self.saving = false; self._fail(e); });
    },
    switchAccount: function (a) {
      var self = this;
      this._confirm('确定把「' + a.name + '」切为当前生效的' + (a.acct_type === 'mp' ? '小程序' : '公众号') + '？\n切换立刻生效，不用重启。', function () {
        self._api('/wx-config/accounts/' + a.id + '/switch', { reason: '后台手动切换' }, 'POST').then(function (d) {
          self._toast((d && d.message) || '已切换'); self.load();
        }).catch(function (e) { self._fail(e); });
      });
    },
    toggleAccount: function (a, active) {
      var self = this;
      this._api('/wx-config/accounts/' + a.id + '/toggle', { active: !!active }, 'POST').then(function (d) {
        self._toast((d && d.message) || '已更新'); self.load();
      }).catch(function (e) { self._fail(e); });
    },
    probe: function (a) {
      var self = this;
      this._toast('正在探活…', 'warning');
      this._api('/wx-config/accounts/' + a.id + '/probe', { mode: 'real' }, 'POST').then(function (d) {
        var r = (d && d.data) || {};
        self._toast((r.ok ? '探活成功：' : '探活失败：') + (r.detail || ''), r.ok ? 'success' : 'error');
        self.load();
      }).catch(function (e) { self._fail(e); });
    },
    removeAccount: function (a) {
      var self = this;
      this._confirm('确定删除「' + a.name + '」？当前生效中的账号不允许删除。', function () {
        self._api('/wx-config/accounts/' + a.id, null, 'DELETE').then(function (d) {
          self._toast((d && d.message) || '已删除'); self.load();
        }).catch(function (e) { self._fail(e); });
      });
    },
    saveCfg: function (c) {
      var self = this;
      this._api('/wx-config/config', { key: c.cfg_key, value: c.cfg_value }, 'POST').then(function () {
        self._toast(c.cfg_key + ' 已保存'); self.load();
      }).catch(function (e) { self._fail(e); });
    }
  }
});
