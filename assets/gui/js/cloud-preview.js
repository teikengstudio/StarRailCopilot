/* Stable viewport, bounded latest-frame display, and explicit viewer/control leases. */
window.startCloudPreview = function (options) {
    'use strict';
    const root = document.getElementById('cloud-preview');
    const find = id => document.getElementById('cloud-' + id);
    const stage = find('stage'), image = find('frame'), overlay = find('overlay');
    const connect = find('connect'), disconnect = find('disconnect');
    const endSession = find('end');
    const screenshot = find('screenshot'), fullscreen = find('fullscreen');
    const readMode = root.querySelector('input[value="read"]');
    const touchMode = root.querySelector('input[value="touch"]');
    const clipboard = find('clipboard'), clipboardText = find('clipboard-text');
    const clipboardButton = clipboard.querySelector('button');
    const pointers = new Map(), moves = new Map();
    let socket, ready = false, wanted = false, attached = false, controlled = false;
    let controlPending = false, disposed = false, reconnectTimer, reconnectAttempt = 0;
    let currentUrl, loadingUrl, queuedFrame, screenshotBlob, decoding = false;
    let receivedAt = 0, remoteAge = null, statusAt = 0, frames = [];
    let status = {state: 'disconnected'}, messageError = '';

    function send(command) {
        if (!ready || !socket || socket.readyState !== WebSocket.OPEN) return false;
        socket.send(JSON.stringify(command));
        return true;
    }

    function touch(action, pointer, point) {
        return send({type: 'touch', action, finger_id: pointer.finger, x: point.x, y: point.y});
    }

    function releasePointers(releaseLease) {
        for (const [id, pointer] of pointers) {
            moves.delete(id);
            pointers.delete(id);
            touch('up', pointer, pointer.point);
            if (stage.hasPointerCapture(id)) stage.releasePointerCapture(id);
        }
        pointers.clear();
        moves.clear();
        if (releaseLease) {
            send({type: 'release'});
            controlPending = false;
            controlled = false;
            readMode.checked = true;
            render();
        }
    }

    function pointAt(event) {
        if (!image.naturalWidth || !image.naturalHeight || !receivedAt) return null;
        const box = image.getBoundingClientRect();
        const scale = Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight);
        const width = image.naturalWidth * scale, height = image.naturalHeight * scale;
        const left = box.left + (box.width - width) / 2;
        const top = box.top + (box.height - height) / 2;
        if (event.clientX < left || event.clientX >= left + width ||
            event.clientY < top || event.clientY >= top + height) return null;
        return {
            x: Math.min(image.naturalWidth - 1, Math.floor((event.clientX - left) / scale)),
            y: Math.min(image.naturalHeight - 1, Math.floor((event.clientY - top) / scale))
        };
    }

    function finishPointer(event) {
        const pointer = pointers.get(event.pointerId);
        if (!pointer) return;
        const point = pointAt(event) || pointer.point;
        moves.delete(event.pointerId);
        touch('up', pointer, point);
        pointers.delete(event.pointerId);
        if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
        event.preventDefault();
    }

    stage.addEventListener('pointerdown', event => {
        if (!controlled || !attached || (event.pointerType === 'mouse' && event.button !== 0)) return;
        const point = pointAt(event);
        if (!point || pointers.has(event.pointerId) || pointers.size >= 10) return;
        const used = new Set(Array.from(pointers.values(), pointer => pointer.finger));
        let finger = 0;
        while (used.has(finger)) finger++;
        const pointer = {finger, point};
        if (!touch('down', pointer, point)) return;
        pointers.set(event.pointerId, pointer);
        stage.setPointerCapture(event.pointerId);
        stage.focus({preventScroll: true});
        event.preventDefault();
    });
    stage.addEventListener('pointermove', event => {
        const pointer = pointers.get(event.pointerId);
        if (!pointer) return;
        const point = pointAt(event);
        if (!point) {
            finishPointer(event);
            return;
        }
        pointer.point = point;
        moves.set(event.pointerId, point);
        event.preventDefault();
    });
    stage.addEventListener('pointerup', finishPointer);
    stage.addEventListener('pointercancel', finishPointer);
    stage.addEventListener('lostpointercapture', finishPointer);
    stage.addEventListener('pointerleave', event => {
        if (pointers.has(event.pointerId) && !pointAt(event)) finishPointer(event);
    });
    stage.addEventListener('contextmenu', event => {
        if (controlled) event.preventDefault();
    });
    const moveTimer = setInterval(() => {
        if (!controlled || !socket || socket.bufferedAmount > 65536) return;
        for (const [id, point] of moves) {
            const pointer = pointers.get(id);
            if (pointer) touch('move', pointer, point);
        }
        moves.clear();
    }, 33);

    function render() {
        connect.disabled = wanted || attached || !ready;
        disconnect.disabled = !wanted && !attached;
        endSession.disabled = !attached || !!status.scheduler;
        endSession.title = status.scheduler ? '脚本拥有云会话' : '立即结束手动云会话';
        touchMode.disabled = !attached || !status.running;
        if (!controlled && !controlPending) readMode.checked = true;
        stage.classList.toggle('cloud-touch-active', controlled);
        clipboardText.disabled = !controlled;
        clipboardButton.disabled = !controlled;
        screenshot.disabled = !screenshotBlob;
        const now = performance.now();
        const age = remoteAge == null ? (receivedAt ? (now - receivedAt) / 1000 : null) :
            remoteAge + (now - statusAt) / 1000;
        const stale = age != null && age > 2;
        const stateText = {disconnected: '未连接', Connecting: '正在连接', Disconnecting: '正在退出',
            Running: '运行中', Stopped: '已停止', Connected: '已连接', Error: '连接失败'};
        const runningState = controlPending ? '等待脚本暂停' :
            controlled ? '触控控制中' : status.paused ? '已暂停' :
            status.pause_requested ? '等待脚本暂停' : stateText[status.state] || status.state || '未连接';
        find('state').textContent = !ready ? '未连接' : runningState;
        root.classList.toggle('cloud-stale', stale);
        overlay.hidden = attached && image.naturalWidth > 0 && !stale;
        overlay.textContent = !attached ? (wanted ? '正在连接' : '未连接') :
            stale ? '画面已过期' : (status.error || runningState);
        const fps = frames.filter(t => now - t < 1000).length;
        const resolution = image.naturalWidth ? image.naturalWidth + ' \u00d7 ' + image.naturalHeight : '\u2014';
        find('metrics').textContent = resolution + ' | ' + fps + ' 帧/秒 | ' +
            (age == null ? '\u2014' : age.toFixed(1) + ' 秒') +
            (status.queue_type ? ' | ' + status.queue_type : '');
        find('error').textContent = messageError || status.error || '';
    }

    function displayFrame(blob) {
        queuedFrame = blob;
        if (decoding) return;
        function next() {
            if (!queuedFrame || disposed) return;
            const frame = queuedFrame;
            queuedFrame = null;
            decoding = true;
            loadingUrl = URL.createObjectURL(frame);
            image.onload = () => {
                if (currentUrl) URL.revokeObjectURL(currentUrl);
                currentUrl = loadingUrl;
                loadingUrl = null;
                screenshotBlob = frame;
                image.hidden = false;
                const now = performance.now();
                receivedAt = now;
                frames = frames.filter(t => now - t < 1000);
                frames.push(now);
                decoding = false;
                render();
                next();
            };
            image.onerror = () => {
                URL.revokeObjectURL(loadingUrl);
                loadingUrl = null;
                decoding = false;
                messageError = '云游戏画面解码失败。';
                render();
                next();
            };
            image.src = loadingUrl;
        }
        next();
    }

    function openSocket() {
        if (disposed) return;
        const url = new URL(options.socketPath, location.href);
        url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        socket = new WebSocket(url.href);
        socket.binaryType = 'blob';
        socket.onopen = () => socket.send(JSON.stringify({type: 'auth', ticket: options.ticket}));
        socket.onmessage = event => {
            if (event.data instanceof Blob) {
                if (attached && document.visibilityState !== 'hidden') displayFrame(event.data);
                return;
            }
            let reply;
            try { reply = JSON.parse(event.data); } catch (_) { return; }
            if (reply.type === 'ready') {
                ready = true;
                reconnectAttempt = 0;
                messageError = '';
                if (wanted) send({type: 'connect'});
            } else if (reply.type === 'status') {
                const hadControl = controlled;
                status = reply;
                attached = !!reply.attached;
                controlled = attached && reply.control_owner === options.viewer;
                if (controlled) {
                    controlPending = false;
                    touchMode.checked = true;
                } else if (hadControl) {
                    releasePointers(false);
                    controlPending = false;
                    readMode.checked = true;
                }
                remoteAge = typeof reply.frame_age === 'number' ? reply.frame_age : null;
                statusAt = performance.now();
            } else if (reply.type === 'control') {
                controlPending = !!reply.pending || !!reply.granted;
                if (!controlPending) {
                    readMode.checked = true;
                    messageError = '触控权限暂不可用或已被其他预览占用。';
                }
            } else if (reply.type === 'error') {
                messageError = reply.error || '云会话错误。';
            }
            render();
        };
        socket.onclose = event => {
            releasePointers(false);
            ready = attached = controlled = controlPending = false;
            readMode.checked = true;
            status = {state: 'disconnected'};
            render();
            if (disposed || event.code === 1008) {
                if (event.code === 1008) {
                    messageError = '会话已过期或访问被拒绝，请重新登录。';
                    render();
                }
                return;
            }
            reconnectTimer = setTimeout(openSocket, Math.min(10000, 500 * 2 ** reconnectAttempt++));
        };
        socket.onerror = () => {
            messageError = '预览连接已中断。';
            render();
        };
    }

    connect.addEventListener('click', () => {
        wanted = true;
        messageError = '';
        send({type: 'connect'});
        render();
    });
    disconnect.addEventListener('click', () => {
        wanted = false;
        releasePointers(true);
        send({type: 'disconnect'});
        attached = false;
        render();
    });
    endSession.addEventListener('click', () => {
        if (!attached || status.scheduler) return;
        wanted = false;
        releasePointers(true);
        send({type: 'end'});
        attached = false;
        render();
    });
    readMode.addEventListener('change', () => { if (readMode.checked) releasePointers(true); });
    touchMode.addEventListener('change', () => {
        if (!touchMode.checked || !attached) return;
        messageError = '';
        controlPending = true;
        send({type: 'control'});
        render();
    });
    clipboard.addEventListener('submit', event => {
        event.preventDefault();
        if (controlled) send({type: 'clipboard', text: clipboardText.value});
    });
    screenshot.addEventListener('click', () => {
        if (!screenshotBlob) return;
        const url = URL.createObjectURL(screenshotBlob);
        const link = document.createElement('a');
        link.href = url;
        link.download = options.config + '-' + new Date().toISOString().replace(/[:.]/g, '-') + '.jpg';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    fullscreen.addEventListener('click', async () => {
        try {
            if (document.fullscreenElement) await document.exitFullscreen();
            else await stage.requestFullscreen();
        } catch (_) {
            messageError = '当前浏览器无法进入全屏。';
            render();
        }
    });
    document.addEventListener('fullscreenchange', () => {
        releasePointers(false);
        const label = document.fullscreenElement ? '退出全屏' : '全屏预览';
        fullscreen.setAttribute('aria-label', label);
        fullscreen.title = label;
    });
    window.addEventListener('blur', () => releasePointers(true));
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') releasePointers(true);
    });
    function dispose() {
        if (disposed) return;
        wanted = false;
        releasePointers(true);
        send({type: 'disconnect'});
        disposed = true;
        clearInterval(moveTimer);
        clearInterval(statusTimer);
        clearTimeout(reconnectTimer);
        queuedFrame = null;
        if (socket) socket.close();
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        if (loadingUrl) URL.revokeObjectURL(loadingUrl);
    }
    window.addEventListener('pagehide', dispose);
    if (window.WebIO && WebIO._state.CurrentSession) {
        WebIO._state.CurrentSession.on_session_close(dispose);
    }
    const statusTimer = setInterval(render, 250);
    openSocket();
    render();
};
