# -*- coding: utf-8 -*-
"""patch_personal.py - 补全个人信息和修改密码页面的加载逻辑"""

HTML_FILE = '/home/ubuntu/smart-locker/static/admin-v2.html'

with open(HTML_FILE, 'r', encoding='utf-8') as f:
    html = f.read()

# 备份
with open(HTML_FILE + '.bak4', 'w', encoding='utf-8') as f:
    f.write(html)

# 1. 替换个人信息组件 - 显示真实角色和创建时间
old_personal = "Vue.component('page-personal-info',{template:'\\n<div class=\"page-box\">\\n<h3>个人信息</h3>\\n<div style=\"max-width:400px;margin-top:20px\">\\n<div class=\"form-group\"><label>用户名</label><input :value=\"$parent.adminUser\" disabled></div>\\n<div class=\"form-group\"><label>角色</label><input value=\"管理员\" disabled></div>\\n</div>\\n</div>'});"

new_personal = """Vue.component('page-personal-info',{template:`
<div class="page-box">
<div class="box-header"><h3>个人信息</h3></div>
<div style="max-width:400px;margin-top:20px">
<div class="form-group"><label>用户名</label><input :value="$parent.adminUser" disabled></div>
<div class="form-group"><label>角色</label><input :value="$parent.profileInfo.role_text" disabled></div>
<div class="form-group"><label>创建时间</label><input :value="$parent.profileInfo.created_at||'-'" disabled></div>
<div class="form-group"><label>用户ID</label><input :value="$parent.profileInfo.id||'-'" disabled></div>
</div>
</div>`,created(){this.$parent.loadProfile();}});"""

html = html.replace(old_personal, new_personal)

# 2. 修改密码组件加created清空表单
old_pwd = "Vue.component('page-change-pwd',{template:'\\n<div class=\"page-box\">\\n<h3>修改密码</h3>\\n<div style=\"max-width:400px;margin-top:20px\">\\n<div class=\"form-group\"><label>旧密码</label><input type=\"password\" v-model=\"$parent.pwdForm.old\" placeholder=\"请输入旧密码\"></div>\\n<div class=\"form-group\"><label>新密码</label><input type=\"password\" v-model=\"$parent.pwdForm.new1\" placeholder=\"请输入新密码\"></div>\\n<div class=\"form-group\"><label>确认密码</label><input type=\"password\" v-model=\"$parent.pwdForm.new2\" placeholder=\"请再次输入新密码\"></div>\\n<button class=\"btn btn-blue\" @click=\"$parent.changePassword()\">确认修改</button>\\n</div>\\n</div>'});"

new_pwd = """Vue.component('page-change-pwd',{template:`
<div class="page-box">
<div class="box-header"><h3>修改密码</h3></div>
<div style="max-width:400px;margin-top:20px">
<div class="form-group"><label>旧密码</label><input type="password" v-model="$parent.pwdForm.old" placeholder="请输入旧密码"></div>
<div class="form-group"><label>新密码</label><input type="password" v-model="$parent.pwdForm.new1" placeholder="请输入新密码"></div>
<div class="form-group"><label>确认密码</label><input type="password" v-model="$parent.pwdForm.new2" placeholder="请再次输入新密码"></div>
<button class="btn btn-blue" @click="$parent.changePassword()">确认修改</button>
</div>
</div>`,created(){this.$parent.pwdForm={old:'',new1:'',new2:''};}});"""

html = html.replace(old_pwd, new_pwd)

# 3. 追加data: profileInfo
old_data = "pwdForm:{old:'',new1:'',new2:''},"
new_data = "pwdForm:{old:'',new1:'',new2:''},profileInfo:{},"
html = html.replace(old_data, new_data)

# 4. 替换menu handler
old_handler = """else if(v==='personal-info')void 0;
else if(v==='change-pwd')void 0;"""
new_handler = """else if(v==='personal-info'){this.loadProfile();}
else if(v==='change-pwd'){this.pwdForm={old:'',new1:'',new2:''};}"""
html = html.replace(old_handler, new_handler)

# 5. 追加loadProfile方法 (在changePassword方法后)
old_method = "changePassword:function(){var self=this;if(!this.pwdForm.old||!this.pwdForm.new1){this.toast('请填写密码','warning');return;}if(this.pwdForm.new1!==this.pwdForm.new2){this.toast('两次密码不一致','error');return;}if(this.pwdForm.new1.length<6){this.toast('新密码至少6位','warning');return;}this.api('/admin/change-password',{old_password:this.pwdForm.old,new_password:this.pwdForm.new1}).then(function(){self.toast('密码修改成功');self.pwdForm={old:'',new1:'',new2:''};}).catch(function(e){self.toast(e.message||'修改失败','error');});},"

new_method = old_method + """
loadProfile:function(){var self=this;this.api('/roles/list').then(function(d){var list=d.data&&d.data.list||[];var me=list.find(function(r){return r.username===self.adminUser;});if(me){self.profileInfo={id:me.id,role_text:me.role==='admin'%s'管理员':me.role==='operator'%s'操作员':'查看者',created_at:me.created_at};}else{self.profileInfo={role_text:'未知',created_at:'-'};}}).catch(function(){self.profileInfo={role_text:'-',created_at:'-'};});},"""

html = html.replace(old_method, new_method)

with open(HTML_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'Done! File size: {len(html)} bytes')
# 验证
assert 'loadProfile' in html, 'loadProfile not found!'
assert 'profileInfo' in html, 'profileInfo not found!'
assert html.count('void 0') == 0, 'still has void 0!'
print('All checks passed!')
