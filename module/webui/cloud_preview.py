"""Authenticated, explicit-attach cloud preview on the existing WebUI server."""
import asyncio
import html
import io
import json
import math
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from PIL import Image
from pywebio.output import put_buttons, put_html, put_image, put_scope, put_text, use_scope
from pywebio.platform.fastapi import webio_routes
from pywebio.session import defer_call, get_current_session, info, run_js, set_env
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from module.config.deep import deep_get
from module.config.utils import alas_instance, filepath_config, read_file
from module.webui.setting import State
from module.webui.utils import Icon, add_css, filepath_css, login


ASSETS = Path(__file__).resolve().parents[2] / 'assets' / 'gui'


def _valid_config(name):
    if not isinstance(name, str) or not name or any(c in name for c in '/\\.'):
        return False
    try:
        return (name in alas_instance() and Path(filepath_config(name)).is_file()
                and deep_get(read_file(filepath_config(name)), 'Alas.Emulator.GameClient') == 'cloud_direct')
    except (OSError, ValueError, TypeError):
        return False


def _same_origin(connection):
    origin = connection.headers.get('origin', '')
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment
                and parsed.netloc.lower() == connection.headers.get('host', '').lower()
                and parsed.scheme == ('https' if connection.url.scheme in ('https', 'wss') else 'http'))
    except ValueError:
        return False


def add_preview_routes(app, key=None, cdn=False):
    """Install a login-gated PyWebIO page and same-origin, capability-gated socket."""
    tickets = {}
    lock = threading.Lock()
    script = (ASSETS / 'js' / 'cloud-preview.js').read_text(encoding='utf-8')

    def preview():
        if key is not None and not login(key):
            put_text('密码错误。')
            return
        name = info.request.path_params['config']
        if not _valid_config(name):
            put_text('云游戏配置不可用。')
            return
        set_env(title=f'SRC | {name}', output_animation=False)
        add_css(filepath_css('alas'))
        add_css(filepath_css('dark-alas' if State.theme == 'dark' else 'light-alas'))
        add_css(filepath_css('cloud-preview'))
        ticket = secrets.token_urlsafe(32)
        viewer = secrets.token_urlsafe(16)
        with lock:
            tickets[ticket] = {'config': name, 'viewer': viewer, 'active': False}
        defer_call(lambda: revoke(ticket))
        escaped = html.escape(name)
        put_html(f'''
<section id="cloud-preview" data-theme="{html.escape(State.theme)}">
  <header class="cloud-header"><a href="/" title="SRC">{Icon.ALAS}<strong>SRC</strong></a>
    <span class="cloud-instance">{escaped}</span><span id="cloud-state" role="status">未连接</span>
  </header>
  <div class="cloud-toolbar" role="toolbar" aria-label="云游戏预览">
    <button id="cloud-connect" class="btn btn-off cloud-icon-button" type="button" title="连接预览" aria-label="连接预览">{Icon.RUN}</button>
    <button id="cloud-disconnect" class="btn btn-off" type="button" title="关闭当前预览" disabled>关闭预览</button>
    <button id="cloud-end" class="btn btn-off" type="button" title="结束手动云会话" disabled>结束云会话</button>
    <fieldset class="cloud-mode"><legend>控制模式</legend>
      <label title="只读预览"><input type="radio" name="cloud-mode" value="read" checked>只读</label>
      <label title="暂停脚本并申请触控"><input type="radio" name="cloud-mode" value="touch" disabled>触控</label>
    </fieldset>
    <button id="cloud-screenshot" class="btn btn-off" type="button" title="保存截图" disabled>截图</button>
    <button id="cloud-fullscreen" class="btn btn-off" type="button" title="全屏预览">全屏</button>
    <span id="cloud-metrics" class="cloud-metrics">&#8212;</span>
  </div>
  <div id="cloud-stage" tabindex="0" aria-label="云游戏画面">
    <img id="cloud-frame" alt="云游戏画面" draggable="false" hidden>
    <span id="cloud-overlay" role="status">未连接</span>
  </div>
  <p id="cloud-error" role="alert"></p>
  <form id="cloud-clipboard" class="cloud-clipboard">
    <input id="cloud-clipboard-text" class="form-control" maxlength="4096" autocomplete="off" aria-label="游戏剪贴板" disabled>
    <button class="btn btn-off" type="submit" title="粘贴到游戏剪贴板" disabled>粘贴</button>
  </form>
</section>''')
        put_scope('cloud_preview_auth')
        run_js(script + '\nwindow.startCloudPreview(options);', options={
            'ticket': ticket, 'viewer': viewer, 'config': name,
            'socketPath': f'/cloud/{quote(name, safe="")}/preview/session',
        })
        from module.device.cloud.login import get_login
        from module.webui.cloud import dismiss_login_challenge, render_cloud_auth
        auth = get_login(name)

        def auth_action(action):
            if action == 'qr':
                auth.begin_qr()
            elif action == 'verify':
                render_cloud_auth(name)
            else:
                auth.cancel()

        page_session = get_current_session()
        shown = None
        rendered_challenge = None
        while not page_session.closed():
            snapshot = auth.snapshot()
            state = snapshot.get('state')
            challenge_id = snapshot.get('challenge_id')
            dismiss_login_challenge(challenge_id)
            qr = snapshot.get('qr_png')
            marker = (state, snapshot.get('error'), qr, challenge_id)
            if marker != shown:
                shown = marker
                with use_scope('cloud_preview_auth', clear=True):
                    if state not in ('idle', 'authenticated', 'cancelled') or qr or challenge_id:
                        put_text({'authenticating': '正在登录', 'qr_waiting': '等待扫码',
                                  'qr_scanned': '已扫码，等待手机确认', 'login_required': '请登录',
                                  'captcha_required': '等待人工验证', 'identity_required': '需要官方身份验证',
                                  'account_restricted': '需要官方账号处理', 'error': '登录失败'}.get(state, state or ''))
                        if snapshot.get('error'):
                            put_text(snapshot['error'])
                        if qr:
                            put_image(qr).style('width: 200px; max-width: 100%;')
                        buttons = [
                            {'label': '扫码登录', 'value': 'qr', 'color': 'off'},
                            {'label': '取消', 'value': 'cancel', 'color': 'off'},
                        ]
                        if challenge_id:
                            buttons.append({'label': '人工验证', 'value': 'verify', 'color': 'off'})
                        put_buttons(buttons, onclick=auth_action)
            if challenge_id and challenge_id != rendered_challenge:
                rendered_challenge = challenge_id
                render_cloud_auth(name)
            time.sleep(0.5)

    def revoke(ticket):
        with lock:
            tickets.pop(ticket, None)

    async def guarded_page(request):
        if request.headers.get('origin') and not _same_origin(request):
            return PlainTextResponse('禁止跨站访问。', status_code=403)
        if not _valid_config(request.path_params['config']):
            return PlainTextResponse('云游戏配置不可用。', status_code=404)
        return await page_http(request)

    async def guarded_page_socket(socket):
        if not _same_origin(socket) or not _valid_config(socket.path_params['config']):
            await socket.close(code=1008)
            return
        await page_socket(socket)

    async def session(socket):
        if not _same_origin(socket) or not _valid_config(socket.path_params['config']):
            await socket.close(code=1008)
            return
        await socket.accept()
        ticket = None
        binding = None
        runtime = None
        attached = False
        fingers = {}
        width, height = 0, 0
        last_frame = None
        state_lock = asyncio.Lock()

        async def release():
            if runtime is None:
                return
            for finger, (x, y) in list(fingers.items()):
                try:
                    await asyncio.to_thread(runtime.input, viewer, {
                        'type': 'touch', 'action': 'up', 'finger_id': finger, 'x': x, 'y': y})
                except Exception:
                    pass  # Runtime also releases every touch when the control lease ends.
            fingers.clear()
            await asyncio.to_thread(runtime.release_control, viewer)

        async def detach():
            nonlocal attached
            if attached:
                try:
                    await release()
                finally:
                    attached = False
                    await asyncio.to_thread(runtime.detach, viewer)

        async def send_frames():
            nonlocal width, height, last_frame
            last_status = 0
            while True:
                started = time.monotonic()
                with lock:
                    authorized = tickets.get(ticket) is binding
                if not authorized:
                    await socket.close(code=1008)
                    return
                async with state_lock:
                    status = (await asyncio.to_thread(runtime.snapshot) if attached else
                              {'state': 'disconnected', 'running': False, 'control_owner': None})
                    if fingers and status.get('control_owner') != viewer:
                        await release()
                    frame = await asyncio.to_thread(runtime.latest_frame) if attached else None
                    new_frame = bool(frame and frame is not last_frame and frame != last_frame)
                    if new_frame:
                        with Image.open(io.BytesIO(frame)) as image:
                            width, height = image.size
                    if started - last_status >= 0.25 or (new_frame and last_frame is None):
                        status.update({'type': 'status', 'attached': attached,
                                       'frame_width': width, 'frame_height': height})
                        await socket.send_json(status)
                        last_status = started
                    if new_frame:
                        last_frame = frame
                        await socket.send_bytes(frame)
                await asyncio.sleep(max(0, 0.1 - (time.monotonic() - started)))

        try:
            first = await asyncio.wait_for(socket.receive_text(), timeout=10)
            auth_message = json.loads(first)
            if not isinstance(auth_message, dict) or auth_message.get('type') != 'auth':
                await socket.close(code=1008)
                return
            ticket = auth_message.get('ticket')
            if not isinstance(ticket, str):
                await socket.close(code=1008)
                return
            with lock:
                binding = tickets.get(ticket)
                if binding and binding['config'] == socket.path_params['config'] and not binding['active']:
                    binding['active'] = True
                else:
                    binding = None
            if binding is None:
                await socket.close(code=1008)
                return
            viewer = binding['viewer']
            await socket.send_json({'type': 'ready', 'viewer': viewer})
            sender = asyncio.create_task(send_frames())
            receiver = asyncio.create_task(socket.receive_text())
            try:
                while True:
                    done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                    if sender in done:
                        await sender
                        break
                    raw = receiver.result()
                    if len(raw) > 32768:
                        await socket.close(code=1009)
                        break
                    command = json.loads(raw)
                    if not isinstance(command, dict):
                        raise ValueError('Invalid command')
                    async with state_lock:
                        kind = command.get('type')
                        if kind == 'connect' and not attached:
                            if not _valid_config(binding['config']):
                                raise ValueError('Cloud profile unavailable')
                            from module.device.cloud.runtime import get_runtime
                            runtime = get_runtime(binding['config'])
                            await asyncio.to_thread(runtime.attach, viewer)
                            attached = True
                            await asyncio.to_thread(runtime.connect)
                        elif kind == 'disconnect':
                            await detach()
                            last_frame = None
                        elif kind == 'end' and attached:
                            status = await asyncio.to_thread(runtime.snapshot)
                            if status.get('scheduler'):
                                await socket.send_json({'type': 'error', 'error': '脚本拥有云会话，仅可关闭预览。'})
                            else:
                                from module.device.cloud.client import CloudConnectionError
                                await release()
                                try:
                                    await asyncio.to_thread(runtime.disconnect)
                                except CloudConnectionError:
                                    await socket.send_json({'type': 'error', 'error': '脚本拥有云会话，请先停止脚本。'})
                                else:
                                    await detach()
                                    last_frame = None
                        elif kind == 'release':
                            await release()
                        elif kind == 'control' and attached:
                            granted = await asyncio.to_thread(runtime.take_control, viewer)
                            status = await asyncio.to_thread(runtime.snapshot)
                            await socket.send_json({'type': 'control', 'granted': bool(granted),
                                                    'pending': status.get('pending_owner') == viewer})
                        elif kind in ('touch', 'clipboard') and attached:
                            status = await asyncio.to_thread(runtime.snapshot)
                            if status.get('control_owner') != viewer:
                                await release()
                                await socket.send_json({'type': 'error', 'error': '尚未获得触控权限。'})
                                receiver = asyncio.create_task(socket.receive_text())
                                continue
                            if kind == 'clipboard':
                                text = command.get('text')
                                if not isinstance(text, str) or len(text) > 4096:
                                    raise ValueError('Invalid clipboard text')
                                await asyncio.to_thread(runtime.input, viewer, {'type': kind, 'text': text})
                            else:
                                finger, action = command.get('finger_id'), command.get('action')
                                x, y = command.get('x'), command.get('y')
                                if (type(finger) is not int or not 0 <= finger < 10
                                        or action not in ('down', 'move', 'up')
                                        or type(x) not in (int, float) or type(y) not in (int, float)
                                        or not math.isfinite(x) or not math.isfinite(y)
                                        or not 0 <= x < width or not 0 <= y < height):
                                    raise ValueError('Invalid touch')
                                if action == 'down' and finger in fingers:
                                    raise ValueError('Touch already active')
                                if action != 'down' and finger not in fingers:
                                    raise ValueError('Touch is not active')
                                await asyncio.to_thread(runtime.input, viewer, {
                                    'type': kind, 'action': action, 'finger_id': finger, 'x': x, 'y': y})
                                if action == 'up':
                                    fingers.pop(finger, None)
                                else:
                                    fingers[finger] = (x, y)
                        else:
                            raise ValueError('Invalid command')
                    receiver = asyncio.create_task(socket.receive_text())
            finally:
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError, asyncio.TimeoutError):
            await socket.close(code=1008)
        except Exception:
            try:
                await socket.send_json({'type': 'error', 'error': '云游戏预览会话失败。'})
                await socket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            try:
                async with state_lock:
                    await detach()
            finally:
                if binding is not None:
                    with lock:
                        binding['active'] = False

    page_routes = webio_routes({'index': preview}, cdn=cdn or '/pywebio_static')
    page_http, page_socket = page_routes[0].endpoint, page_routes[1].endpoint
    app.router.routes[0:0] = [
        Route('/cloud/{config}/preview', guarded_page),
        WebSocketRoute('/cloud/{config}/preview', guarded_page_socket),
        WebSocketRoute('/cloud/{config}/preview/session', session),
    ]
