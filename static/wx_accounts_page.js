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
        <td class="wx-ops">
          <button class="btn-text green" v-if="!a.is_active" :disabled="a.usable===false" :title="a.usable_reason" @click="switchAccount(a)">设为生效</button>
          <button class="btn-text orange" v-if="a.is_active" @click="toggleAccount(a,false)">停用</button>
          <button class="btn-text blue" v-if="!a.is_active" :disabled="a.usable===false" :title="a.usable_reason" @click="toggleAccount(a,true)">启用</button>
          <button class="btn-text blue" :disabled="a.usable===false" :title="a.usable_reason" @click="probe(a)">探活</button>
          <button class="btn-text green" v-if="a.acct_type==='mp'" :disabled="a.usable===false||syncing" :title="'拉取该小程序的订阅模板字段并自动生成映射（换小程序后点一下就行）'" @click="syncTemplates(a)">同步模板</button>
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
    <thead><tr><th>业务</th><th>通道</th><th>模板ID</th><th>字段映射<span class="wx-tag on" style="margin-left:4px" title="后台可配：换小程序后点账号行的「同步模板」自动生成">可配</span></th><th>落地页</th><th>说明</th></tr></thead>
    <tbody>
      <tr v-for="t in templates" :key="t.id">
        <td>{{t.biz_label}}<div class="wx-mono" style="color:#909399">{{t.biz}}</div></td>
        <td><span class="wx-tag" :class="t.channel==='mp'?'on':'warn'">{{t.channel==='mp'?'小程序订阅':'公众号模板'}}</span></td>
        <td class="wx-mono" style="max-width:300px">{{t.template_id}}</td>
        <td style="white-space:normal;max-width:300px;font-size:12.5px">
          <template v-if="t.field_map && Object.keys(t.field_map).length">
            <span v-for="(v,k) in t.field_map" :key="k" class="wx-tag on" style="margin:1px 3px 1px 0">{{k}}→{{v}}</span>
          </template>
          <span v-else class="wx-tag warn" title="没配 -> 代码里硬编码的兜底表生效；换小程序容易 47003">未配（用硬编码）</span>
        </td>
        <td class="wx-mono" style="color:#909399">{{t.page||'-'}}</td>
        <td style="white-space:normal;max-width:340px;color:#909399;font-size:12.5px">{{t.note}}</td>
      </tr>
      <tr v-if="!templates.length"><td colspan="6" class="empty-row">暂无模板</td></tr>
    </tbody>
  </table>

  <div class="wx-section-title">微信校验文件（普通链接二维码规则 / 业务域名 / JS安全域名）
    <button class="btn-text blue" style="margin-left:8px" @click="loadVerifyFiles()">刷新</button>
  </div>
  <div class="wx-tip">
    微信要求把校验文件放到<b>域名根目录</b>（如 <code>https://你的域名/xxxx.txt</code>）。
    下面上传后会自动落到服务器 <code>static/</code>，nginx 已有根路径规则，<b>立即可访问</b>，不用再 SSH。<br>
    用法：微信后台点「校验」前，先把文件内容贴进来保存 → 再点校验。
  </div>
  <table>
    <thead><tr><th>文件名</th><th>大小</th><th>可访问地址</th><th>内容预览</th><th></th></tr></thead>
    <tbody>
      <tr v-for="f in verifyFiles" :key="f.filename">
        <td class="wx-mono">{{f.filename}}</td>
        <td>{{f.size}} 字节</td>
        <td class="wx-mono"><a :href="f.url" target="_blank" style="color:#1677FF">{{f.url}}</a></td>
        <td class="wx-mono" style="max-width:320px;white-space:normal;color:#909399">{{f.preview}}</td>
        <td><button class="btn-text blue" @click="useVerifyFile(f)">编辑</button></td>
      </tr>
      <tr v-if="!verifyFiles.length"><td colspan="5" class="empty-row">还没有校验文件</td></tr>
    </tbody>
  </table>
  <div class="wx-cfg-add" style="margin-top:10px;padding:10px;border:1px dashed #dcdfe6;border-radius:6px">
    <div class="form-row">
      <div class="form-group"><label>文件名（含后缀）</label>
        <input v-model="vfName" placeholder="例：yf7rvDz0Er.txt">
        <div class="form-hint">只允许字母/数字/下划线/点/横线，后缀 .txt 或 .html</div>
      </div>
    </div>
    <div class="form-group"><label>文件内容</label>
      <textarea v-model="vfContent" rows="3" placeholder="校验文件里的那串字符，例：b2dc9557155789e88db307531c517cd2"></textarea>
    </div>
    <div style="text-align:right">
      <button class="btn btn-sm btn-blue" :disabled="vfSaving" @click="saveVerifyFile">{{vfSaving?'保存中…':'保存并部署'}}</button>
    </div>
  </div>

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

  <div class="modal-mask" v-if="syncResult" @click.self="closeSyncResult">
    <div class="modal-box md" style="max-width:900px">
      <div class="modal-header">
        <h3>模板字段同步结果 —— {{syncResult.account_name}}（{{syncResult.appid}}）</h3>
        <span class="close" @click="closeSyncResult">×</span>
      </div>
      <div class="modal-body">
        <div class="wx-tip" style="margin-bottom:12px">
          <b>自动匹配原理</b>：代码发送字段有固定中文语义（如 amount2=退款金额、thing4=退款方式），
          微信模板每行形如「退款金额:{{amount8.DATA}}」，按<b>中文标签</b>比对即可自动得出映射。<br>
          <b>结果</b>：{{syncResult.applied ? '✅ 已写入数据库，立刻生效（不用重启）' : '⚠️ 只预览未写入'}}
        </div>
        <table>
          <thead><tr><th>业务</th><th>微信字段（标签）</th><th>自动映射</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="t in syncResult.templates" :key="t.biz">
              <td>{{t.biz}}<div class="wx-mono" style="color:#909399">{{t.title||''}}</div></td>
              <td style="white-space:normal;font-size:12.5px">
                <span v-for="f in (t.tpl_fields||[])" :key="f.key" class="wx-tag" style="margin:1px 3px 1px 0">{{f.key}}={{f.label}}</span>
                <span v-if="!(t.tpl_fields||[]).length" style="color:#909399">-</span>
              </td>
              <td style="white-space:normal;font-size:12.5px">
                <span v-for="(v,k) in (t.field_map||{})" :key="k" class="wx-tag on" style="margin:1px 3px 1px 0">{{k}}→{{v}}</span>
                <span v-if="!Object.keys(t.field_map||{}).length" style="color:#909399">-</span>
              </td>
              <td>
                <span v-if="t.status==='ok'" class="wx-tag on">{{t.saved?'已保存':'匹配成功'}}</span>
                <span v-else-if="t.status==='wx_not_found'" class="wx-tag off" :title="t.note">微信里没找到</span>
                <span v-else-if="t.status==='no_match'" class="wx-tag warn">没匹配上</span>
                <span v-else class="wx-tag">{{t.status}}</span>
              </td>
            </tr>
            <tr v-if="!syncResult.templates.length"><td colspan="4" class="empty-row">该账号还没有配模板</td></tr>
          </tbody>
        </table>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm" @click="closeSyncResult">关闭</button>
      </div>
    </div>
  </div>

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

          <div class="form-group"><label>退款成功通知 模板ID（可留空，填了自动生效）</label>
            <input v-model="form.tpl_refund" placeholder="例：rnaDY96hollmN6V-yP1CXrNw4cIq_3mAOne6rYEmq7U">
            <div class="form-hint">填了会写入该账号的 subscribe_refund 模板；之后点行内「同步模板」自动生成字段映射</div>
          </div>
          <div class="form-group"><label>账户余额变动通知 模板ID（可留空）</label>
            <input v-model="form.tpl_general" placeholder="例：bScYz5t6fLiZimvbu-Egjw5lj4xjsRtPv6zydnzvJ8U">
          </div>
          <div class="form-group"><label>微信校验文件名（含后缀，可留空）</label>
            <input v-model="form.verify_file_name" placeholder="例：E2MKyK1lH2.txt">
            <div class="form-hint">只允许 .txt / .html；保存后自动部署到域名根目录，立即可访问</div>
          </div>
          <div class="form-group"><label>校验文件内容</label>
            <textarea v-model="form.verify_file_content" rows="2" placeholder="校验文件里的那串字符，例：9f6a06449c3355fe3fb830ffa50344d3"></textarea>
          </div>
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
    // [S1011-20261015] 微信校验文件
    verifyFiles: [], vfName: '', vfContent: '', vfSaving: false,
    // [S1009-20261015] 模板字段同步
    syncResult: null, syncing: false,
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
  created: function () { this.load(true); this.loadVerifyFiles(); },
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

    /* ---------- [S1009-20261015] 模板字段同步（换小程序后点一下就行） ---------- */
    syncTemplates: function (a) {
      var self = this;
      if (self.syncing) { return; }
      self.syncing = true;
      self._toast('正在拉取微信模板并自动匹配字段…');
      return this._api('/admin/wx-template-sync', { account_id: a.id, apply: true }, 'POST').then(function (d) {
        self.syncing = false;
        self.syncResult = (d && d.data) ? d.data : d;
        var n = ((self.syncResult || {}).templates || []).filter(function (t) { return t.status === 'ok'; }).length;
        self._toast('同步完成：' + n + ' 个模板匹配成功' + (n ? '，已保存并立刻生效' : '（请检查微信后台是否已申请模板）'));
        self.load();
      }).catch(function (e) {
        self.syncing = false;
        self._fail(e);
      });
    },
    closeSyncResult: function () { this.syncResult = null; },

    /* ---------- [S1011-20261015] 微信校验文件 ---------- */
    loadVerifyFiles: function () {
      var self = this;
      return this._api('/admin/wechat-verify-file', null, 'GET').then(function (d) {
        var x = (d && d.data) ? d.data : (d || {});
        self.verifyFiles = x.files || [];
      }).catch(function () { /* 静默 */ });
    },
    useVerifyFile: function (f) {
      this.vfName = f.filename;
      this.vfContent = f.preview || '';
      this._toast('已填入表单，改完点「保存并部署」');
    },
    saveVerifyFile: function () {
      var self = this;
      var n = String(this.vfName || '').trim();
      if (!n) { self._toast('请填文件名', 'error'); return; }
      self.vfSaving = true;
      return this._api('/admin/wechat-verify-file',
                       { filename: n, content: String(this.vfContent || '') }, 'POST')
        .then(function (d) {
          self.vfSaving = false;
          var x = (d && d.data) ? d.data : (d || {});
          self.verifyFiles = x.files || self.verifyFiles;
          self._toast((d && d.message) || '已部署');
        }).catch(function (e) {
          self.vfSaving = false;
          self._fail(e);
        });
    },

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
                    mch_relation: 'pending', priority: 100, token: '', note: '', openid_prefix: '',
                    tpl_refund: '', tpl_general: '', verify_file_name: '', verify_file_content: '' };
      this.formTitle = '添加' + (this.form.acct_type === 'mp' ? '小程序' : '公众号');
      this.modal = true;
    },
    openEdit: function (a) {
      this.form = Object.assign({}, a);
      this.form.tpl_refund = a.tpl_refund || '';
      this.form.tpl_general = a.tpl_general || '';
      this.form.verify_file_name = a.verify_file_name || '';
      this.form.verify_file_content = a.verify_file_content || '';
      this.formTitle = '编辑：' + a.name;
      this.modal = true;
      this._loadAcctTemplates(a.id);
    },
    // [S871] 编辑时把该账号已配的两个模板ID回填到弹窗
    _loadAcctTemplates: function (accountId) {
      var self = this;
      return this._api('/wx-config/templates', null, 'GET').then(function (d) {
        var list = (d && d.data) ? d.data : (d || []);
        if (!Array.isArray(list)) { return; }
        list.forEach(function (t) {
          if (Number(t.account_id) !== Number(accountId)) { return; }
          if (t.biz === 'subscribe_refund' && !self.form.tpl_refund) { self.form.tpl_refund = t.template_id || ''; }
          if (t.biz === 'subscribe_general' && !self.form.tpl_general) { self.form.tpl_general = t.template_id || ''; }
        });
      }).catch(function () { /* 静默 */ });
    },
    closeModal: function () { this.modal = false; },
    saveAccount: function () {
      var self = this, f = this.form, isNew = !f.id;
      this.saving = true;
      var body = {
        acct_type: f.acct_type, name: f.name, appid: f.appid, secret: f.secret,
        subject: f.subject, mch_relation: f.mch_relation, priority: f.priority,
        token: f.token, note: f.note, openid_prefix: f.openid_prefix,
        tpl_refund: f.tpl_refund || '', tpl_general: f.tpl_general || '',
        verify_file_name: f.verify_file_name || '', verify_file_content: f.verify_file_content || ''
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
      this._confirm('确定把「' + a.name + '」切为当前生效的' + (a.acct_type === 'mp' ? '小程序' : '公众号') + '？\n切换前会自动探活（拿新号真连一次微信），探活不过不会切；切了立刻生效，不用重启。', function () {
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
