"""Persistent threshold rules and background notification delivery."""
import copy
import json
import math
import queue
import re
import socket
import threading
import time
from http.client import HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request


class AlertError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


DEFAULT_CONFIG = {
    "enabled": False, "channel": "wecom", "credential": "",
    "duration_seconds": 60, "repeat_seconds": 900,
    "rules": {"cpu": {"enabled": True, "threshold": 80},
              "memory": {"enabled": True, "threshold": 90},
              "disk": {"enabled": True, "threshold": 90}},
}
LABELS = {"cpu": "CPU 使用率", "memory": "内存使用率", "disk": "磁盘使用率"}


def validate_credential(channel, credential):
    if not credential:
        return
    if len(credential) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in credential):
        raise AlertError("通知凭据格式无效")
    if channel == "serverchan":
        if not re.fullmatch(r"SCT[A-Za-z0-9]{8,180}", credential):
            raise AlertError("请输入 Server酱 Turbo 的 SCT 开头 SendKey")
        return
    try:
        url = urlsplit(credential)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.fragment and url.port != 0)
        if channel == "wecom":
            query = parse_qs(url.query)
            valid = (valid and url.hostname == "qyapi.weixin.qq.com" and url.port in (None, 443)
                     and url.path == "/cgi-bin/webhook/send" and set(query) == {"key"}
                     and len(query["key"]) == 1 and bool(query["key"][0]))
    except ValueError:
        valid = False
    if not valid:
        raise AlertError("请输入有效的企业微信机器人 Webhook" if channel == "wecom" else "Webhook 必须是无用户名、密码或片段的 HTTPS 地址")


def validated_config(previous, data):
    allowed = set(DEFAULT_CONFIG) | {"clear_credential"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise AlertError("存在不支持的告警配置项")
    result = copy.deepcopy(previous)
    for key in ("enabled", "clear_credential"):
        if key in data and type(data[key]) is not bool:
            raise AlertError("告警开关必须是布尔值")
    channel = data.get("channel", result["channel"])
    if channel not in ("wecom", "serverchan", "webhook"):
        raise AlertError("不支持的通知渠道")
    if channel != result["channel"] or data.get("clear_credential"):
        result["credential"] = ""
    result["channel"] = channel
    if "credential" in data:
        if not isinstance(data["credential"], str):
            raise AlertError("通知凭据必须是字符串")
        if data["credential"].strip():
            result["credential"] = data["credential"].strip()
    if "enabled" in data:
        result["enabled"] = data["enabled"]
    for key, low, high in (("duration_seconds", 0, 3600), ("repeat_seconds", 60, 86400)):
        value = data.get(key, result[key])
        if type(value) is not int or not low <= value <= high:
            raise AlertError(f"{('持续时间' if key == 'duration_seconds' else '重复通知间隔')}需为 {low}–{high} 秒的整数")
        result[key] = value
    if "rules" in data:
        rules = data["rules"]
        if not isinstance(rules, dict) or set(rules) - set(LABELS):
            raise AlertError("不支持的监控指标")
        for metric, rule in rules.items():
            if not isinstance(rule, dict) or set(rule) - {"enabled", "threshold"}:
                raise AlertError("告警规则格式无效")
            merged = {**result["rules"][metric], **rule}
            threshold = merged["threshold"]
            if type(merged["enabled"]) is not bool:
                raise AlertError("规则开关必须是布尔值")
            if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 1 <= threshold <= 100:
                raise AlertError("阈值需为 1–100 的百分比")
            result["rules"][metric] = merged
    validate_credential(channel, result["credential"])
    if result["enabled"] and not result["credential"]:
        raise AlertError("请先配置通知渠道凭据，再开启告警")
    return result


_POST_SLOTS = threading.BoundedSemaphore(2)


def _post_https(request, timeout=5):
    """Bound total delivery time, including servers dripping headers or body.

    Cancellation shuts down the actual socket to interrupt blocking reads.
    The bounded slots also cap outstanding DNS lookups, which Python cannot
    cancel. A late lookup checks cancellation before sending any request.
    """
    url = urlsplit(request.full_url)
    connection = HTTPSConnection(url.hostname, url.port or 443, timeout=timeout)
    if not _POST_SLOTS.acquire(blocking=False):
        raise AlertError("通知连接仍在结束，请稍后重试", 503)
    completed, cancelled = threading.Event(), threading.Event()
    result = {}
    socket_holder = {}

    def perform():
        try:
            connection.connect()
            socket_holder["socket"] = connection.sock
            if cancelled.is_set():
                return
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            connection.request(request.get_method(), path, body=request.data, headers=dict(request.header_items()))
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise AlertError(f"通知接口返回 HTTP {response.status}", 502)
            result["body"] = response.read(65537)
        except Exception as exc:
            result["error"] = exc
        finally:
            try:
                connection.close()
            finally:
                _POST_SLOTS.release()
                completed.set()

    try:
        threading.Thread(target=perform, daemon=True, name="monilite-notify-http").start()
    except Exception:
        _POST_SLOTS.release()
        raise
    if not completed.wait(timeout):
        cancelled.set()
        active_socket = socket_holder.get("socket")
        if active_socket:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise AlertError("通知接口发送超时，请检查服务器网络", 502)
    if "error" in result:
        raise result["error"]
    return result["body"]


def send_notification(config, event):
    """Do not propagate provider text or request URLs containing credentials."""
    channel, credential = config["channel"], config["credential"]
    validate_credential(channel, credential)
    if not credential:
        raise AlertError("尚未配置通知渠道")
    content_type = "application/json; charset=utf-8"
    if channel == "serverchan":
        url = f"https://sctapi.ftqq.com/{credential}.send"
        payload = urlencode({"title": event["title"], "desp": event["message"]}).encode()
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
    else:
        url = credential
        body = {"msgtype": "text", "text": {"content": event["message"]}} if channel == "wecom" else event
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = Request(url, data=payload, headers={"Content-Type": content_type, "User-Agent": "MoniLite-alerts/1"}, method="POST")
    try:
        raw = _post_https(request, timeout=5)
        if len(raw) > 65536:
            raise AlertError("通知接口响应过大", 502)
        if channel != "webhook":
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AlertError("通知接口返回无效数据", 502)
            if channel == "wecom":
                success = type(result.get("errcode")) is int and result["errcode"] == 0
            else:
                details = result.get("data")
                success = (type(result.get("code")) is int and result["code"] == 0
                           and (details is None or isinstance(details, dict) and details.get("errno", 0) == 0))
            if not success:
                raise AlertError("通知服务拒绝了消息，请检查凭据、额度及渠道配置", 502)
    except HTTPError as exc:
        exc.close()
        raise AlertError(f"通知接口返回 HTTP {exc.code}", 502) from None
    except (URLError, TimeoutError, OSError):
        raise AlertError("通知接口连接失败或超时，请检查服务器网络", 502) from None
    except (ValueError, UnicodeDecodeError):
        raise AlertError("通知接口返回无效数据", 502) from None


class AlertService:
    def __init__(self, auth, sender=None):
        self.auth = auth
        self.sender = sender or send_notification
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.states = {}
        self.version = 0
        self.thread = None
        self.delivery_thread = None
        self.pending = queue.Queue(maxsize=32)
        self.last_sample_at = None
        with auth.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                channel TEXT NOT NULL, event TEXT NOT NULL, success INTEGER NOT NULL, error TEXT)""")
            row = db.execute("SELECT value FROM settings WHERE key='alerts'").fetchone()
        self.config = json.loads(row[0]) if row else copy.deepcopy(DEFAULT_CONFIG)

    def status(self):
        with self.lock:
            config = copy.deepcopy(self.config)
            config["credential_configured"] = bool(config.pop("credential"))
            active = [copy.deepcopy(item["event"]) for item in self.states.values()]
        with self.auth.connection() as db:
            history = [{"id": row["id"], "timestamp": row["timestamp"], "channel": row["channel"],
                        "event": json.loads(row["event"]), "success": bool(row["success"]), "error": row["error"]}
                       for row in db.execute("SELECT * FROM alert_deliveries ORDER BY id DESC LIMIT 100")]
        return {"config": config, "active": active, "history": history}

    def update(self, data):
        with self.lock:
            config = validated_config(self.config, data)
            with self.auth.connection(write=True) as db:
                db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('alerts',?)",
                           (json.dumps(config, allow_nan=False),))
            if config != self.config:
                self.states.clear()
                self.last_sample_at = None
                self.version += 1
            self.config = config
        return self.status()

    def record(self, config, event, success, error):
        with self.auth.connection(write=True) as db:
            db.execute("INSERT INTO alert_deliveries (timestamp,channel,event,success,error) VALUES (?,?,?,?,?)",
                       (time.time(), config["channel"], json.dumps(event, ensure_ascii=False), int(success), error))
            db.execute("DELETE FROM alert_deliveries WHERE id NOT IN (SELECT id FROM alert_deliveries ORDER BY id DESC LIMIT 100)")

    def deliver(self, config, event):
        error = None
        try:
            self.sender(config, event)
        except AlertError as exc:
            # Only errors from our transport are safe to expose. Custom senders
            # (including tests) must not leak arbitrary exception text.
            error = str(exc) if self.sender is send_notification else "通知发送失败，请检查渠道配置"
        except Exception:
            error = "通知发送失败，请检查渠道配置"
        self.record(config, event, error is None, error)
        return {"success": error is None, "error": error}

    def test_notification(self, hostname):
        if not self.delivery_lock.acquire(blocking=False):
            raise AlertError("正在发送其他通知，请稍后再试", 409)
        try:
            with self.lock:
                config = copy.deepcopy(self.config)
            if not config["credential"]:
                raise AlertError("请先保存通知渠道凭据")
            event = {"kind": "test", "hostname": hostname, "timestamp": time.time(),
                     "title": "MoniLite 测试通知", "message": f"MoniLite 测试通知\n服务器：{hostname}\n通知渠道已连通。"}
            return self.deliver(config, event)
        finally:
            self.delivery_lock.release()

    def evaluate(self, snapshot, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            config, version = copy.deepcopy(self.config), self.version
            timestamp = snapshot.get("timestamp", 0) if snapshot else 0
            if not config["enabled"] or not snapshot or not 0 <= time.time() - timestamp <= 10:
                self.states.clear()
                self.last_sample_at = None
                return
            if self.last_sample_at is not None and timestamp - self.last_sample_at > 10:
                self.states.clear()
            self.last_sample_at = timestamp
            values = [("cpu", "", snapshot["cpu"]["percent"]), ("memory", "", snapshot["memory"]["percent"])]
            values.extend(("disk", disk["mount"], disk["percent"]) for disk in snapshot.get("disks", []))
            present, pending = set(), []
            for metric, mount, value in values:
                key = (metric, mount)
                rule = config["rules"][metric]
                if not rule["enabled"] or not math.isfinite(value) or value <= rule["threshold"]:
                    continue
                present.add(key)
                item = self.states.setdefault(key, {"since": now, "last_attempt": None, "queued": False, "event": {}})
                duration = max(0, int(now - item["since"]))
                label = LABELS[metric] + (f" {mount}" if mount else "")
                hostname = snapshot["system"]["hostname"]
                event = {"kind": "alert", "metric": metric, "mount": mount, "hostname": hostname,
                         "value": value, "threshold": rule["threshold"], "duration_seconds": duration,
                         "timestamp": timestamp, "title": f"MoniLite · {label}超限",
                         "message": f"MoniLite 资源告警\n服务器：{hostname}\n指标：{label}\n当前值：{value:g}%\n阈值：{rule['threshold']:g}%\n持续时间：{duration} 秒\n采集时间：{time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(timestamp))}"}
                item["event"] = event
                if not item["queued"] and duration >= config["duration_seconds"] and (item["last_attempt"] is None or now - item["last_attempt"] >= config["repeat_seconds"]):
                    item["last_attempt"] = now
                    if self.delivery_thread is not None:
                        try:
                            self.pending.put_nowait((version, key, item))
                            item["queued"] = True
                        except queue.Full:
                            item["last_attempt"] = None
                    else:
                        pending.append(event)
            self.states = {key: value for key, value in self.states.items() if key in present}
        for event in pending:
            with self.delivery_lock:
                with self.lock:
                    if version != self.version:
                        return
                if time.time() - event["timestamp"] > 10:
                    return
                self.deliver(config, event)

    def start(self, monitor):
        if self.thread is not None:
            return

        def delivery_loop():
            while not monitor.stop_event.is_set():
                try:
                    version, key, queued_item = self.pending.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    with self.delivery_lock:
                        with self.lock:
                            item = self.states.get(key)
                            if version != self.version or item is not queued_item:
                                continue
                            item["queued"] = False
                            if monitor.stop_event.is_set() or time.time() - item["event"]["timestamp"] > 10:
                                continue
                            item["last_attempt"] = time.monotonic()
                            event, config = copy.deepcopy(item["event"]), copy.deepcopy(self.config)
                        self.deliver(config, event)
                except Exception:
                    print("Alert delivery failed; check notification history", flush=True)
                finally:
                    self.pending.task_done()

        def loop():
            while not monitor.stop_event.is_set():
                try:
                    with monitor.lock:
                        snapshot = copy.deepcopy(monitor.current)
                    self.evaluate(snapshot)
                except Exception:
                    # Never log raw transport or configuration exceptions.
                    print("Alert evaluation failed; will retry on the next sample", flush=True)
                monitor.stop_event.wait(2)

        self.delivery_thread = threading.Thread(target=delivery_loop, daemon=True, name="monilite-alert-delivery")
        self.delivery_thread.start()
        self.thread = threading.Thread(target=loop, daemon=True, name="monilite-alerts")
        self.thread.start()

    def stop(self):
        if self.thread:
            self.thread.join(timeout=6)
        if self.delivery_thread:
            self.delivery_thread.join(timeout=6)
