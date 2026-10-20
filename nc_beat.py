#!/usr/bin/env python3
"""
osu! Beat Sound  -  NMのままNC風のビート音を鳴らすアプリ

仕組み:
  tosu (osuのメモリリーダー) の WebSocket から「曲の再生位置」を受け取り、
  譜面(.osu)のタイミングポイントから計算したビート時刻に合わせて、
  sounds フォルダのサウンドを鳴らします。osu本体には一切触りません。

使い方:
  1. tosu を起動しておく  (https://github.com/KotRikD/tosu)
  2. sounds フォルダに kick / clap / hat / finish の wav/ogg/mp3 を入れる
  3. python nc_beat.py   (GUIなしなら  python nc_beat.py --nogui)
"""
import argparse, bisect, json, os, sys, threading, time, urllib.request, urllib.parse
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "tosu_url": "ws://127.0.0.1:24050/websocket/v2",
    "tosu_http": "http://127.0.0.1:24050",
    "sounds_dir": "sounds",
    "offset_ms": 0,            # +で早く鳴らす / -で遅く鳴らす (音声遅延の補正用)
    "volume": 0.8,
    "bpm_scale": 1.0,          # 0.5=半分 / 1=そのまま / 2=2倍 (160BPM譜面を320で刻みたい等)
    "enabled_sounds": {"kick": True, "clap": True, "hat": True, "finish": True},
    "play_states": ["play"],   # このゲーム状態のときだけ鳴らす (例: "play","selectplay" 等)
    "skip_if_nc": True,        # 本物のNCモッド中は二重に鳴らさない
    # 4/4拍子のパターン: 1小節=8ステップ(8分音符)。各サウンドを鳴らすステップ番号
    "pattern_4_4": {
        "kick":   [0, 4],
        "clap":   [2, 6],
        "hat":    [1, 3, 5, 7],
        "finish": [0],
    },
}
SOUND_EXTS = (".wav", ".ogg", ".mp3", ".flac")
SOUND_ALIASES = {  # ファイル名の候補 (拡張子なし, 小文字)
    "kick":   ["kick", "nightcore-kick"],
    "clap":   ["clap", "nightcore-clap"],
    "hat":    ["hat", "nightcore-hat"],
    "finish": ["finish", "nightcore-finish"],
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[config] 読み込み失敗: {e}")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ───────────────────────── 譜面解析 ─────────────────────────
def parse_timing_points(text):
    """[TimingPoints] から 赤線(uninherited)だけ取り出す -> [(time_ms, beat_len_ms, meter)]"""
    pts, in_sec = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_sec = line == "[TimingPoints]"
            continue
        if not in_sec or not line or line.startswith("//"):
            continue
        p = line.split(",")
        try:
            t, bl = float(p[0]), float(p[1])
            meter = int(p[2]) if len(p) > 2 else 4
            unin = int(p[6]) if len(p) > 6 else (1 if bl > 0 else 0)
        except ValueError:
            continue
        if unin == 1 and bl > 0:
            pts.append((t, bl, max(meter, 1)))
    pts.sort()
    return pts


def build_events(pts, end_ms, pattern, scale=1.0):
    """ビートのイベント表を作る -> (times[], sounds[])  (ミリ秒昇順)"""
    times, sounds = [], []
    for i, (t0, bl, meter) in enumerate(pts):
        bl = bl / scale
        t_end = pts[i + 1][0] if i + 1 < len(pts) else end_ms
        step_len = bl / 2.0                      # 8分音符
        spm = meter * 2                          # 1小節のステップ数
        n = 0
        while True:
            t = t0 + n * step_len
            if t >= t_end - 1e-6 or len(times) > 400000:
                break
            s = n % spm
            if meter == 4:
                names = [k for k, steps in pattern.items() if s in steps]
            else:  # 4/4以外は汎用: 頭=kick+finish, 他の拍=clap, 裏=hat
                if s == 0:
                    names = ["kick", "finish"]
                elif s % 2 == 0:
                    names = ["clap"]
                else:
                    names = ["hat"]
            if names:
                times.append(t)
                sounds.append(names)
            n += 1
    return times, sounds


# ───────────────────────── 音声 ─────────────────────────
class SoundBank:
    def __init__(self, folder):
        import pygame
        self.pg = pygame
        pygame.mixer.pre_init(44100, -16, 2, 256)  # 小さいバッファ = 低遅延
        pygame.mixer.init()
        pygame.mixer.set_num_channels(32)
        self.folder, self.sounds, self.volume = folder, {}, 0.8
        self.reload()

    def reload(self):
        self.sounds.clear()
        os.makedirs(self.folder, exist_ok=True)
        files = {os.path.splitext(f)[0].lower(): f for f in os.listdir(self.folder)
                 if f.lower().endswith(SOUND_EXTS)}
        for key, names in SOUND_ALIASES.items():
            for n in names:
                if n in files:
                    try:
                        self.sounds[key] = self.pg.mixer.Sound(os.path.join(self.folder, files[n]))
                        break
                    except Exception as e:
                        print(f"[sound] {files[n]} を読めません: {e}")
        self.set_volume(self.volume)
        return sorted(self.sounds)

    def set_volume(self, v):
        self.volume = v
        for s in self.sounds.values():
            s.set_volume(v)

    def play(self, key):
        s = self.sounds.get(key)
        if s:
            s.play()


# ───────────────────────── エンジン ─────────────────────────
class Engine:
    def __init__(self, cfg, bank):
        self.cfg, self.bank = cfg, bank
        self.lock = threading.Lock()
        self.stop_flag = False
        self.status = {"conn": "未接続", "map": "-", "state": "-", "events": 0, "pos": 0}
        # 譜面
        self.map_key = None
        self.times, self.sounds = [], []
        # 時刻推定
        self.samples = deque()      # (wall, live - wall*rate)
        self.est = None
        self.rate = 1.0
        self.live = 0.0
        self.last_live = None
        self.last_change_wall = 0.0
        self.paused = True
        self.active = False         # 鳴らして良い状態か
        self.last_pos = None

    # ---- tosu からのメッセージ処理 ----
    def on_message(self, data):
        now = time.perf_counter()
        state = str(data.get("state", {}).get("name", "")).lower()
        mods = str(data.get("play", {}).get("mods", {}).get("name", "")).upper()
        mods_rate = data.get("play", {}).get("mods", {}).get("rate")
        rate = float(mods_rate) if isinstance(mods_rate, (int, float)) and mods_rate else 1.0
        if mods_rate is None:
            tokens = [mods[i:i + 2] for i in range(0, len(mods), 2)]
            rate = 1.5 if ("DT" in tokens or "NC" in tokens) else 0.75 if "HT" in tokens else 1.0
        bm = data.get("beatmap", {})
        live = float(bm.get("time", {}).get("live", 0))
        allowed = state in [s.lower() for s in self.cfg["play_states"]]
        is_nc = "NC" in [mods[i:i + 2] for i in range(0, len(mods), 2)]
        with self.lock:
            self.status["state"] = state or "-"
            self.active = allowed and not (self.cfg["skip_if_nc"] and is_nc)
            # 譜面が変わったら読み込む
            key = (bm.get("checksum") or bm.get("id"), bm.get("title"), data.get("files", {}).get("beatmap"))
            if key != self.map_key:
                self.map_key = key
                self.times, self.sounds = [], []
                self.last_pos = None
                threading.Thread(target=self.load_map, args=(data, key), daemon=True).start()
            # 時刻の推定 (遅れて届いたメッセージの影響を、窓内の最大値で打ち消す)
            if rate != self.rate:
                self.rate = rate
                self.samples.clear(); self.est = None
            if self.last_live is not None and live != self.last_live:
                self.last_change_wall = now
                self.paused = False
            self.last_live = live
            if now - self.last_change_wall > 0.2:
                self.paused = True
            self.live = live
            off = live - now * 1000.0 * self.rate
            if self.est is not None and abs(off - self.est) > 250:   # シーク/リトライ
                self.samples.clear(); self.est = None; self.last_pos = None
            self.samples.append((now, off))
            while self.samples and now - self.samples[0][0] > 1.5:
                self.samples.popleft()
            self.est = max(o for _, o in self.samples)

    def load_map(self, data, key):
        try:
            text = self.read_osu(data)
            pts = parse_timing_points(text)
            end = float(data.get("beatmap", {}).get("time", {}).get("mp3Length") or 0) or 600000.0
            times, sounds = build_events(pts, end, self.cfg["pattern_4_4"], self.cfg["bpm_scale"])
            with self.lock:
                if self.map_key == key:
                    self.pts, self.end = pts, end
                    self.times, self.sounds = times, sounds
                    self.status["events"] = len(times)
                    self.status["map"] = (data.get("beatmap", {}).get("title") or "?")
            print(f"[map] {self.status['map']}  赤線={len(pts)}  イベント={len(times)}")
        except Exception as e:
            print(f"[map] 譜面の読み込みに失敗: {e}")
            with self.lock:
                self.status["map"] = "読み込み失敗"

    def read_osu(self, data):
        candidates = []
        dp = data.get("directPath", {}).get("beatmapFile")
        if dp:
            candidates.append(dp)
            candidates.append(os.path.join(data.get("folders", {}).get("songs", ""), dp))
        fo, fi = data.get("folders", {}), data.get("files", {})
        if fi.get("beatmap"):
            candidates.append(os.path.join(fo.get("songs", ""), fo.get("beatmap", ""), fi["beatmap"]))
        for p in candidates:
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8-sig", errors="replace") as f:
                    return f.read()
        # ローカルで見つからなければ tosu のHTTP経由で取得
        if dp or fi.get("beatmap"):
            for rel in filter(None, [dp, os.path.join(fo.get("beatmap", ""), fi.get("beatmap", ""))]):
                url = self.cfg["tosu_http"] + "/files/beatmap/" + urllib.parse.quote(rel.replace("\\", "/"))
                try:
                    with urllib.request.urlopen(url, timeout=3) as r:
                        return r.read().decode("utf-8-sig", "replace")
                except Exception:
                    pass
        raise FileNotFoundError("譜面ファイルが見つかりません")

    def rebuild(self):
        """BPM倍率が変わったときにビート表を作り直す"""
        with self.lock:
            if not getattr(self, "pts", None):
                return
            self.times, self.sounds = build_events(self.pts, self.end, self.cfg["pattern_4_4"], self.cfg["bpm_scale"])
            self.status["events"] = len(self.times)
            self.last_pos = None

    # ---- 再生ループ ----
    def current_pos(self, now):
        if self.est is None:
            return None
        if self.paused:
            return self.live
        return self.est + now * 1000.0 * self.rate

    def play_loop(self):
        while not self.stop_flag:
            now = time.perf_counter()
            with self.lock:
                pos = self.current_pos(now)
                if pos is not None:
                    pos += self.cfg["offset_ms"] * self.rate
                times, sounds, active = self.times, self.sounds, self.active
                due = []
                if pos is not None:
                    self.status["pos"] = pos
                    lp = self.last_pos
                    if lp is not None and active and not self.paused and times and 0 <= pos - lp < 300:
                        i, j = bisect.bisect_right(times, lp), bisect.bisect_right(times, pos)
                        for k in range(i, j):
                            due.extend(sounds[k])
                    self.last_pos = pos
            for name in due:
                if self.cfg["enabled_sounds"].get(name, True):
                    self.bank.play(name)
            time.sleep(0.001)

    # ---- WebSocket ----
    def ws_loop(self):
        import websocket
        while not self.stop_flag:
            try:
                self.status["conn"] = "接続中..."
                ws = websocket.create_connection(self.cfg["tosu_url"], timeout=5)
                ws.settimeout(2)
                self.status["conn"] = "接続OK"
                while not self.stop_flag:
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    try:
                        self.on_message(json.loads(msg))
                    except Exception as e:
                        print("[msg]", repr(e))
                ws.close()
            except Exception as e:
                self.status["conn"] = "tosuに接続できません (起動してますか?)"
                time.sleep(2)

    def start(self):
        if sys.platform == "win32":
            try:
                import ctypes; ctypes.windll.winmm.timeBeginPeriod(1)  # sleep精度を1msに
            except Exception:
                pass
        threading.Thread(target=self.play_loop, daemon=True).start()
        threading.Thread(target=self.ws_loop, daemon=True).start()


# ───────────────────────── GUI ─────────────────────────
def run_gui(engine, bank, cfg):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("osu! Beat Sound")
    root.geometry("420x420")
    pad = {"padx": 10, "pady": 4}
    v_conn, v_map, v_state, v_snd = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
    for v, label in ((v_conn, "tosu"), (v_state, "状態"), (v_map, "譜面"), (v_snd, "読込済み音")):
        row = ttk.Frame(root); row.pack(fill="x", **pad)
        ttk.Label(row, text=label, width=10).pack(side="left")
        ttk.Label(row, textvariable=v, wraplength=290).pack(side="left")

    def slider(text, lo, hi, init, cb, fmt):
        row = ttk.Frame(root); row.pack(fill="x", **pad)
        ttk.Label(row, text=text, width=10).pack(side="left")
        lab = ttk.Label(row, width=8); lab.pack(side="right")
        def f(x):
            x = float(x); lab.config(text=fmt(x)); cb(x)
        s = ttk.Scale(row, from_=lo, to=hi, command=f); s.set(init); s.pack(side="left", fill="x", expand=True)
        f(init)
    slider("音量", 0, 1, cfg["volume"], lambda x: (cfg.update(volume=x), bank.set_volume(x)), lambda x: f"{int(x*100)}%")
    slider("タイミング", -150, 150, cfg["offset_ms"], lambda x: cfg.update(offset_ms=int(x)), lambda x: f"{int(x)} ms")

    row = ttk.Frame(root); row.pack(fill="x", **pad)
    ttk.Label(row, text="BPM倍率", width=10).pack(side="left")
    var_scale = tk.DoubleVar(value=cfg["bpm_scale"])
    def set_scale():
        cfg["bpm_scale"] = var_scale.get(); engine.rebuild()
    for txt, val in (("x0.5", 0.5), ("x1", 1.0), ("x2", 2.0)):
        ttk.Radiobutton(row, text=txt, value=val, variable=var_scale, command=set_scale).pack(side="left", padx=6)

    ttk.Label(root, text="鳴らす音").pack(anchor="w", **pad)
    box = ttk.Frame(root); box.pack(fill="x", **pad)
    for name in ("kick", "clap", "hat", "finish"):
        var = tk.BooleanVar(value=cfg["enabled_sounds"].get(name, True))
        ttk.Checkbutton(box, text=name, variable=var,
                        command=lambda n=name, v=var: cfg["enabled_sounds"].__setitem__(n, v.get())).pack(side="left", padx=6)
        ttk.Button(box, text="▶", width=2, command=lambda n=name: bank.play(n)).pack(side="left")
    var_always = tk.BooleanVar(value="selectplay" in cfg["play_states"])
    def toggle_sel():
        st = [s for s in cfg["play_states"] if s != "selectplay"]
        if var_always.get(): st.append("selectplay")
        cfg["play_states"] = st
    ttk.Checkbutton(root, text="曲選択画面でも鳴らす", variable=var_always, command=toggle_sel).pack(anchor="w", **pad)

    def reload():
        v_snd.set(", ".join(bank.reload()) or "なし (soundsフォルダに入れてください)")
    ttk.Button(root, text="サウンドを再読み込み", command=reload).pack(**pad)
    reload()

    def tick():
        s = engine.status
        v_conn.set(s["conn"]); v_state.set(s["state"]); v_map.set(f'{s["map"]} ({s["events"]}拍)')
        root.after(300, tick)
    tick()
    def close():
        engine.stop_flag = True; save_config(cfg); root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nogui", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    folder = cfg["sounds_dir"] if os.path.isabs(cfg["sounds_dir"]) else os.path.join(HERE, cfg["sounds_dir"])
    bank = SoundBank(folder)
    bank.set_volume(cfg["volume"])
    engine = Engine(cfg, bank)
    engine.start()
    if not args.nogui:
        try:
            import tkinter  # noqa
        except ImportError:
            print("tkinterが無いためGUIなしで起動します")
            args.nogui = True
    if args.nogui:
        print("読込済みの音:", sorted(bank.sounds) or "なし")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        run_gui(engine, bank, cfg)


if __name__ == "__main__":
    main()
