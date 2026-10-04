"""Native SRC settings and shared human cloud-login dialogs."""

import json
import threading
from functools import partial

from pywebio.exceptions import SessionException
from pywebio.io_ctrl import output_register_callback
from pywebio.output import (
    clear, close_popup, popup, put_buttons, put_collapse, put_error, put_html,
    put_image, put_link, put_scope, put_text, toast, use_scope,
)
from pywebio.pin import pin, pin_update, put_file_upload
from pywebio.session import eval_js, local, run_js

from module.webui.utils import Icon
from module.webui.widgets import put_arg_input, put_arg_textarea


BROWSER_PROFILE_JS = r"""
(async () => {
    const n = navigator;
    const device = {}, browser = {}, protocol = {}, platform = {};
    const assign = (obj, key, value) => {
        if (value !== undefined && value !== null && value !== '') obj[key] = value;
    };
    assign(browser, 'user_agent', n.userAgent);
    assign(protocol, 'sdk_webview_ua', n.userAgent);
    assign(browser, 'platform', n.platform);
    assign(device, 'cpu_cores', n.hardwareConcurrency);
    assign(device, 'memory_gb', n.deviceMemory);
    assign(device, 'screen_width', screen.width);
    assign(device, 'screen_height', screen.height);
    assign(browser, 'device_pixel_ratio', window.devicePixelRatio);
    if (screen.deviceXDPI) device.dpi = screen.deviceXDPI;
    else if (window.devicePixelRatio) device.dpi = 96 * window.devicePixelRatio;
    let ua = null;
    if (n.userAgentData) {
        ua = n.userAgentData.toJSON ? n.userAgentData.toJSON() : {
            brands: n.userAgentData.brands,
            mobile: n.userAgentData.mobile,
            platform: n.userAgentData.platform
        };
        if (n.userAgentData.getHighEntropyValues) {
            try {
                const high = await Promise.race([
                    n.userAgentData.getHighEntropyValues([
                        'architecture', 'bitness', 'model', 'platformVersion', 'fullVersionList'
                    ]),
                    new Promise(resolve => setTimeout(() => resolve({}), 3000))
                ]);
                ua = Object.assign({}, ua, high);
            } catch (_) {}
        }
        browser.user_agent_data = ua;
        if (ua.platform) platform.sec_ch_ua_platform = JSON.stringify(ua.platform);
        if (typeof ua.mobile === 'boolean') platform.sec_ch_ua_mobile = ua.mobile ? '?1' : '?0';
        if (ua.brands && ua.brands.length) {
            browser.sec_ch_ua = ua.brands.map(b =>
                JSON.stringify(b.brand) + ';v=' + JSON.stringify(b.version)).join(', ');
        }
        assign(device, 'model', ua.model);
    }
    const os = (ua && ua.platform) || n.platform;
    assign(device, 'os', os);
    if (ua && ua.platformVersion) device.sys_version = os + ' ' + ua.platformVersion;
    else assign(device, 'sys_version', os);
    assign(browser, 'web_device_os', os ? encodeURIComponent(os) : null);
    const brand = ua && (ua.fullVersionList || ua.brands || []).find(b =>
        !/not.?a.?brand/i.test(b.brand) && b.brand !== 'Chromium');
    if (brand) {
        browser.web_device_name = brand.brand;
        browser.web_device_model = encodeURIComponent(brand.brand + ' ' + brand.version);
    }
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            assign(device, 'gpu_model', gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
            const lose = gl.getExtension('WEBGL_lose_context');
            if (lose) lose.loseContext();
        }
    } catch (_) {}
    return {device_profile: device, browser_profile: browser,
            protocol_profile: protocol, platform_profile: platform};
})()
"""


GEETEST_JS = r"""
(async () => {
    const target = document.getElementById(element_id);
    if (!target) return;
    const config = challenge.data;
    let used = false;
    const fail = () => { if (target.isConnected) target.textContent = '验证未完成，请重试或取消登录。'; };
    const finish = validation => {
        if (used || !target.isConnected || Date.now() / 1000 >= challenge.expires_at) return;
        if (!validation) { fail(); return; }
        used = true;
        WebIO.pushData({challenge_id: challenge_id, result: validation}, callback_id);
    };
    const load = (src, name) => new Promise((resolve, reject) => {
        if (typeof window[name] === 'function') { resolve(); return; }
        const script = document.createElement('script');
        script.src = src;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
    });
    const ready = captcha => {
        const watcher = new MutationObserver(() => {
            if (!target.isConnected) {
                if (typeof captcha.destroy === 'function') captcha.destroy();
                watcher.disconnect();
            }
        });
        watcher.observe(document.body, {childList: true, subtree: true});
        captcha.onReady(() => {
            if (!target.isConnected) return;
            if (config.use_v4) captcha.showCaptcha(); else captcha.verify();
        });
        captcha.onSuccess(() => finish(captcha.getValidate()));
        captcha.onError(fail);
        captcha.onClose(fail);
    };
    try {
        if (config.use_v4) {
            await load(v4_url, 'initGeetest4');
            if (!target.isConnected) return;
            initGeetest4({captchaId: config.gt, riskType: config.risk_type,
                timeout: 10000, language: 'zho', product: 'bind', hideSuccess: true,
                userInfo: JSON.stringify({session_id: challenge.session_id}), onError: fail}, ready);
        } else {
            await load(v3_url, 'initGeetest');
            if (!target.isConnected) return;
            initGeetest({width: '100%', lang: 'zh-cn', timeout: 10000,
                api_server: 'apiv6.geetest.com', gt: config.gt, challenge: config.challenge,
                new_captcha: config.new_captcha, product: 'bind', offline: !config.success,
                onError: fail}, ready);
        }
    } catch (_) { if (target.isConnected) target.textContent = '极验组件加载失败，请重试或使用扫码登录。'; }
})();
"""


def render_login_challenge(login):
    """Open the current human challenge in this authenticated PyWebIO session."""
    from module.device.cloud.account import CloudAccountError
    from module.device.cloud.core.aigis import GEETEST_V3_JS_URL, GEETEST_V4_JS_URL
    snapshot = login.snapshot()
    challenge, challenge_id = snapshot['challenge'], snapshot['challenge_id']
    if not challenge or challenge.get('type') != 'geetest' or not challenge_id:
        return False
    element_id = 'cloud-captcha-' + challenge_id

    def submit(payload):
        try:
            if not isinstance(payload, dict) or set(payload) != {'challenge_id', 'result'}:
                raise CloudAccountError('Invalid human verification response.', kind='captcha')
            if payload['challenge_id'] != challenge_id:
                raise CloudAccountError('Human verification belongs to another challenge.', kind='captcha')
            login.submit_captcha(challenge_id, payload['result'])
            close_popup()
        except CloudAccountError:
            toast('验证结果无效、已使用或已过期，请重新登录。', color='error')

    def cancel():
        login.cancel(challenge_id=challenge_id)
        close_popup()

    callback_id = output_register_callback(submit)
    local.cloud_challenge_id = challenge_id
    popup('人工验证', [put_html('<div id="%s"></div>' % element_id),
                       put_buttons([dict(label='取消登录', value='cancel', color='off')],
                                   onclick=lambda _: cancel())], closable=False)
    run_js(GEETEST_JS, element_id=element_id, challenge=challenge, challenge_id=challenge_id,
           callback_id=callback_id, v4_url=GEETEST_V4_JS_URL, v3_url=GEETEST_V3_JS_URL)
    return True


def dismiss_login_challenge(current_id=None):
    if getattr(local, 'cloud_challenge_id', None) not in (None, current_id):
        local.cloud_challenge_id = None
        close_popup()


def render_cloud_auth(config_name):
    from module.device.cloud.login import get_login
    return render_login_challenge(get_login(config_name))


_LOGIN_TEXT = {
    'idle': '未登录', 'authenticating': '正在登录', 'qr_waiting': '等待扫码',
    'qr_scanned': '已扫码，等待手机确认', 'authenticated': '已登录',
    'login_required': '请登录', 'captcha_required': '等待人工极验',
    'qr_expired': '二维码已过期，请刷新',
    'identity_required': '需要官方身份或短信验证', 'account_restricted': '需要官方账号处理',
    'cancelled': '已取消', 'error': '登录失败',
}
_LOGIN_BUSY = frozenset({'authenticating', 'qr_waiting', 'qr_scanned', 'captcha_required'})


class CloudPanel:
    def __init__(self, gui):
        self.gui = gui
        self._lock = threading.RLock()
        self._mounted = self._active = False
        self._editing = None
        self._generation = 0
        self._rendered_state = None
        self._shown_challenge = None

    def mount(self, task, mode):
        self.close()
        self._mounted = True
        self._config_name = self.gui.alas_name
        self._mode_pin = f'{task}_Emulator_GameClient'
        put_scope('cloud_panel')
        put_scope('cloud_nav', scope='navigator')
        self.select_mode(self._mode_pin, mode)
        self.gui.task_handler.add(self.poll, 0.5, pending_delete=True)

    def select_mode(self, name, value):
        if not self._mounted or name != self._mode_pin or value == 'cloud_direct' and self._active:
            return
        self._generation += 1
        self._active = value == 'cloud_direct'
        self._rendered_state = self._shown_challenge = None
        self._editing = None
        clear('cloud_panel')
        clear('cloud_nav')
        if not self._active:
            return
        try:
            from module.device.cloud.login import get_login
            self._login = get_login(self._config_name)
            self._account = self._login.account
            saved = self._account.read()
        except Exception:
            self._active = False
            put_error('无法读取本地云游戏配置。', scope='cloud_panel')
            return
        with use_scope('cloud_panel'):
            with use_scope('cloud_nav'):
                for target, title in [('cloud_login_group', '云游戏登录'),
                                      ('cloud_device_group', '云游戏设备'),
                                      ('cloud_advanced_group', '云游戏高级设置')]:
                    put_buttons([dict(label=title, value=target, color='navigator')],
                                onclick=self._navigate)
            with use_scope('cloud_login_group'):
                self._heading('云游戏登录', Icon.SETTING)
                put_scope('cloud_state')
                put_scope('cloud_qr')
                self._buttons([('扫码登录 / 刷新', 'qr', 'on'), ('取消登录', 'cancel', 'off')])
                put_scope('cloud_verify')
                with use_scope('cloud_password_edit'):
                    put_collapse('账号密码登录', [
                        self._input('cloud_account', '账号', '', autocomplete='off'),
                        self._input('cloud_password', '密码', '', type='password', autocomplete='new-password'),
                        self._button_output([('保存加密凭据并登录', 'password', 'on'),
                                             ('使用已保存凭据登录', 'password_login', 'on'),
                                             ('清除已保存密码', 'clear_password', 'off')]),
                    ])
                put_scope('cloud_saved')
                put_link('打开独立触控预览', url=f'/cloud/{self._account.config_name}/preview', new_window=True)
            with use_scope('cloud_device_group'):
                self._heading('设备', Icon.SETTING)
                with use_scope('cloud_device_edit'):
                    device = saved['profile'].get('device_profile', {})
                    for key, title in [('device_id', '设备 ID'), ('model', '型号'), ('os', '系统'),
                                       ('gpu_model', 'GPU'), ('screen_width', '屏幕宽度'),
                                       ('screen_height', '屏幕高度')]:
                        self._input('cloud_device_' + key, title, device.get(key, ''),
                                    type='number' if key.startswith('screen_') else 'text')
                    self._buttons([('采集当前浏览器设备', 'collect', 'on'), ('保存设备', 'device', 'on')])
            with use_scope('cloud_advanced_group'):
                self._heading('高级', Icon.DEVELOP)
                with use_scope('cloud_advanced_edit'):
                    put_collapse('Cookie 与设备画像 JSON', [
                        self._input('cloud_cookie', 'Cookie', '', type='password', autocomplete='off'),
                        self._button_output([('保存 Cookie', 'cookie', 'on')]),
                        put_scope('cloud_upload', [self._upload_input()]),
                        self._button_output([('导入 credentials.json', 'import_cookie', 'on')]),
                        put_arg_textarea(dict(name='cloud_profile', title='设备画像 JSON', rows=12,
                                              value=json.dumps(saved['profile'], ensure_ascii=False, indent=2))),
                        self._button_output([('保存设备画像', 'profile', 'on')]),
                    ])
        self._saved_status(saved)
        self.poll()

    @staticmethod
    def _navigate(target):
        run_js("const groups=$('#pywebio-scope-groups');"
               "const section=$('#pywebio-scope-'+target);"
               "if(section.length)groups.scrollTop(section.position().top+groups.scrollTop()-59);",
               target=target)

    @staticmethod
    def _heading(title, icon):
        put_html(icon).style('width:24px;height:24px;display:inline-block;vertical-align:middle')
        put_text(title).style('display:inline-block;vertical-align:middle;margin-left:8px')
        put_html('<hr class="hr-group">')

    @staticmethod
    def _input(name, title, value, **kwargs):
        return put_arg_input(dict(name=name, title=title, value=value, **kwargs))

    @staticmethod
    def _upload_input():
        return put_scope('arg_container-input-cloud_credentials', [
            put_text('credentials.json').style('--arg-title--'),
            put_file_upload('cloud_credentials', accept='.json', max_size='64K', placeholder='选择文件'),
        ])

    def _button_output(self, items):
        return put_buttons([dict(label=label, value=action, color=color) for label, action, color in items],
                           onclick=partial(self.action, generation=self._generation))

    def _buttons(self, items):
        self._button_output(items).show()

    def _scheduler_running(self):
        return bool(getattr(getattr(self.gui, 'alas', None), 'alive', False))

    def _saved_status(self, saved=None):
        saved = saved if saved is not None else self._account.read()
        password = saved.get('password_login') or {}
        with use_scope('cloud_saved', clear=True):
            put_text('加密密码凭据：' + ('已保存' if password.get('account') and password.get('password') else '未保存'))
            put_text('Cookie：' + ('已保存' if saved.get('cookie') else '未保存'))

    def _refresh_profile(self):
        profile = self._account.read()['profile']
        pin_update('cloud_profile', value=json.dumps(profile, ensure_ascii=False, indent=2))
        for key in ('device_id', 'model', 'os', 'gpu_model', 'screen_width', 'screen_height'):
            pin_update('cloud_device_' + key, value=profile.get('device_profile', {}).get(key, ''))

    def action(self, action, generation):
        with self._lock:
            if generation != self._generation or not self._active or self._editing is not None:
                return
            self._editing = generation
        try:
            if action == 'cancel':
                self._login.cancel()
                return
            if action == 'verify':
                render_login_challenge(self._login)
                return
            if action == 'qr':
                self._login.begin_qr()
                return
            if action == 'password_login':
                self._login.begin_password()
                return
            if self._scheduler_running() or self._login.snapshot()['state'] in _LOGIN_BUSY:
                toast('运行或登录期间不能修改云配置，请先停止或取消。', color='error')
                return
            if action == 'password':
                value = (pin['cloud_account'] or '', pin['cloud_password'] or '')
            elif action == 'cookie':
                value = pin['cloud_cookie'] or ''
            elif action == 'import_cookie':
                upload = pin['cloud_credentials']
                if not upload:
                    toast('请选择 credentials.json。', color='error')
                    return
                value = json.loads(upload['content'].decode('utf-8-sig'))
            elif action == 'collect':
                value = eval_js(BROWSER_PROFILE_JS)
            elif action == 'profile':
                value = json.loads(pin['cloud_profile'] or '')
            elif action == 'device':
                value = {key: pin['cloud_device_' + key] for key in
                         ('device_id', 'model', 'os', 'gpu_model', 'screen_width', 'screen_height')}
                for key in ('screen_width', 'screen_height'):
                    value[key] = int(value[key] or 0)
                value = {'device_profile': value}
            else:
                value = None
            with self._lock:
                if generation != self._generation or not self._active:
                    return
                if self._scheduler_running() or self._login.snapshot()['state'] in _LOGIN_BUSY:
                    return
                if action == 'password':
                    self._account.save_password(*value)
                    self._login.begin_password()
                elif action == 'clear_password':
                    self._account.clear_password()
                elif action == 'cookie':
                    self._account.save_cookie(value)
                elif action == 'import_cookie':
                    if not isinstance(value, dict) or not isinstance(value.get('cookie'), str):
                        raise ValueError('Invalid credentials JSON')
                    self._account.save_cookie(value['cookie'])
                elif action in ('collect', 'profile', 'device'):
                    if not isinstance(value, dict):
                        raise ValueError('Invalid device profile')
                    if action == 'collect':
                        value.get('device_profile', {}).pop('device_id', None)
                    self._account.save_profile(value)
                    self._refresh_profile()
                self._saved_status()
                toast('已保存。', color='success')
        except SessionException:
            return
        except Exception:
            toast('操作失败，请检查输入及本地云游戏配置。', color='error')
        finally:
            with self._lock:
                if self._editing == generation:
                    self._editing = None
            if generation == self._generation and self._active:
                try:
                    if action in ('password', 'clear_password'):
                        pin_update('cloud_account', value='')
                        pin_update('cloud_password', value='')
                    elif action == 'cookie':
                        pin_update('cloud_cookie', value='')
                    elif action == 'import_cookie':
                        with use_scope('cloud_upload', clear=True):
                            self._upload_input().show()
                    self.poll()
                except SessionException:
                    pass

    def poll(self):
        if not self._active or not self.gui.alive:
            return
        snapshot = self._login.snapshot()
        dismiss_login_challenge(snapshot['challenge_id'])
        scheduler = self._scheduler_running()
        state = (snapshot['state'], snapshot['error'], snapshot['challenge_id'],
                 snapshot['qr_png'], scheduler, self._editing)
        if state == self._rendered_state:
            return
        self._rendered_state = state
        with use_scope('cloud_state', clear=True):
            put_text(_LOGIN_TEXT.get(snapshot['state'], snapshot['state']))
            if snapshot['error']:
                put_error(snapshot['error'])
            if scheduler:
                put_text('调度器运行中')
        with use_scope('cloud_qr', clear=True):
            if snapshot['qr_png']:
                put_image(snapshot['qr_png']).style('width:240px;max-width:100%;height:auto')
        with use_scope('cloud_verify', clear=True):
            if snapshot['challenge_id']:
                self._buttons([('打开人工验证', 'verify', 'on')])
            elif snapshot['challenge'] and snapshot['challenge'].get('official_url'):
                put_link('前往官方账号页面处理', url=snapshot['challenge']['official_url'], new_window=True)
        disabled = scheduler or self._editing is not None or snapshot['state'] in _LOGIN_BUSY
        run_js("$('#pywebio-scope-cloud_password_edit,#pywebio-scope-cloud_device_edit,#pywebio-scope-cloud_advanced_edit')"
               ".find('input,textarea,select,button').prop('disabled', disabled)", disabled=disabled)
        if snapshot['challenge_id'] and snapshot['challenge_id'] != self._shown_challenge:
            self._shown_challenge = snapshot['challenge_id']
            render_login_challenge(self._login)
        if snapshot['state'] == 'authenticated':
            self._saved_status()

    def close(self):
        # This page is not a runtime viewer; leaving settings must not stop shared authentication.
        with self._lock:
            self._mounted = self._active = False
            self._editing = None
            self._generation += 1
