#!/usr/bin/env python3
"""終電寝過ごし「詰み度」動画ジェネレーター

駅ごとの設定ファイル(JSON)から、点数・原稿・字幕付き動画(MP4)を作る。

    python render.py stations/otsuki.json            # out/otsuki.mp4 ほかを出力
    python render.py stations/*.json                 # まとめて生成
    python render.py stations/otsuki.json --no-video # 原稿と確認用HTMLだけ(速い)
    python render.py stations/otsuki.json --voicevox 3        # VOICEVOXで読み上げを合成し、声の長さに動画を合わせる
    python render.py stations/otsuki.json --voice-dir rec/    # 自分で収録した1文ごとの音声に動画を合わせる
    python render.py stations/otsuki.json --audio narration.wav  # 1本の音声をそのまま重ねるだけ

必要なもの: Python 3.9+ / pip install playwright / playwright install chromium / ffmpeg
"""
import argparse, base64, hashlib, json, math, re, subprocess, sys, urllib.parse, urllib.request, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEIGHTS = [5, 4, 4, 2, 2, 3]            # 待ち時間, タクシー, 宿, 暇つぶし, コンビニ, 気温
AXES = ["待ち時間", "タクシー", "宿", "暇つぶし", "コンビニ", "気温"]
CHANNEL = "GeogramJP"
TAGLINE = "地図データを面白く可視化"


# ---------- 採点 ----------
def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def score_wait(minutes):          # 3時間30分未満=1点、15分ごとに+1、4時間15分以上=5点
    return 1 if minutes < 210 else 2 if minutes < 225 else 3 if minutes < 240 else 4 if minutes < 255 else 5


def score_taxi(yen):
    return 1 if yen < 5000 else 2 if yen < 10000 else 3 if yen < 20000 else 4 if yen < 30000 else 5


def score_count(n):               # 宿・暇つぶし・コンビニ共通: 4軒以上=1点、1軒減るごとに+1、0軒=5点
    return 1 if n >= 4 else 2 if n == 3 else 3 if n == 2 else 4 if n == 1 else 5


score_stay = score_count


def score_temp(c):                # 3℃以上=1点、1℃下がるごとに+1、0℃未満=5点
    return 1 if c >= 3 else 2 if c >= 2 else 3 if c >= 1 else 4 if c >= 0 else 5


def verdict(total):
    return "詰み" if total >= 80 else "キツい" if total >= 70 else "気合いと根性" if total >= 60 else "耐え" if total >= 50 else "辛くない"


# ---------- 文言の整形 ----------
def jt(s):
    h, m = s.split(":")
    return f"{int(h)}時{int(m)}分"


def yen(n):
    return f"約{int(n):,}円"


def deg(c):
    n = abs(c)
    n = int(n) if float(n).is_integer() else n
    return f"マイナス{n}度" if c < 0 else f"{n}度"


def num(n):
    return int(n) if float(n).is_integer() else n


def shops(st, key, unit):
    """店舗の言い方: 複数なら「○○など3軒」、1つなら「○○の1軒」、0なら「なし」。5軒以上は数を出さず「5軒以上」"""
    n, name, more = st[key], st.get(key + "_name", "").strip(), st[key] >= 5
    if n == 0:
        return "なし"
    tail = f"5{unit}以上" if more else f"{n}{unit}"
    if not name:
        return tail
    return f"{name}など{tail}" if n >= 2 or more else f"{name}の{tail}"


def item(st, label, key, unit, cue, delay=0):
    return [label, min(st[key], 5), unit, cue, delay, None, st.get(key + "_name", "").strip(), st[key] >= 5]


def stop(text):                   # 自由記述のコメントは必ず句点で終える(字幕の区切りに使うため)
    text = text.strip()
    return text if not text or text.endswith("。") else text + "。"


def map_view(points, box_w, box_h, min_span, fill, center=None):
    """点群が枠に収まる地図の範囲(経度の左右端と中心緯度)を決める"""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    latc = (min(lats) + max(lats)) / 2
    cs = math.cos(math.radians(latc))
    need_w = (max(lons) - min(lons)) / fill
    need_h = (max(lats) - min(lats)) / fill * box_w / box_h / cs
    span = max(min_span, need_w, need_h)
    lonc = center[1] if center else (min(lons) + max(lons)) / 2
    if center:
        latc = center[0]
    return {"lon0": lonc - span / 2, "lon1": lonc + span / 2, "latc": latc}


# ---------- 路線データからルートを作る ----------
_RAIL = None


def rail():
    global _RAIL
    if _RAIL is None:
        _RAIL = json.loads((HERE / "rail.json").read_text(encoding="utf-8"))
    return _RAIL


def km(a, b):                     # a, b = [経度, 緯度]
    cs = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * cs, a[1] - b[1]) * 111.2


def nearest(seg, pt):
    i = min(range(len(seg)), key=lambda k: km(seg[k], pt))
    return i, km(seg[i], pt)


def path_len(pts):
    return sum(km(pts[k], pts[k + 1]) for k in range(len(pts) - 1))


def slice_seg(seg, i, j):
    """線形の i 番目から j 番目までを取り出す。環状線は短い側を通る"""
    direct = seg[i:j + 1] if i <= j else seg[j:i + 1][::-1]
    if len(seg) > 3 and km(seg[0], seg[-1]) < 0.3:
        around = (seg[i:] + seg[1:j + 1]) if i > j else (seg[:i + 1][::-1] + seg[j:-1][::-1])
        if path_len(around) < path_len(direct):
            return around
    return direct


def trace_segment(line, a, b):
    """1路線の中で駅aから駅bまでの線形を取り出す。枝分かれ路線は2本の枝をつないで探す"""
    segs = line["segs"]
    best = None
    for seg in segs:
        (i, di), (j, dj) = nearest(seg, a), nearest(seg, b)
        if best is None or di + dj < best[0]:
            best = (di + dj, slice_seg(seg, i, j))
    if best and best[0] < 1.0:
        return best[1]
    for sa in segs:                # 乗り入れなどで枝をまたぐ場合
        for sb in segs:
            if sa is sb:
                continue
            (i, di), (j, dj) = nearest(sa, a), nearest(sb, b)
            joins = [(nearest(sb, sa[e])[1], e, nearest(sb, sa[e])[0]) for e in (0, len(sa) - 1)]
            joins += [(nearest(sa, sb[e])[1], nearest(sa, sb[e])[0], e) for e in (0, len(sb) - 1)]
            gap, ja, jb = min(joins)
            cost = di + dj + gap
            if cost < best[0]:
                best = (cost, slice_seg(sa, i, ja) + slice_seg(sb, jb, j))
    if best and best[0] < 2.0:
        return best[1]
    return [a, b]                  # 線形が見つからなければ直線で結ぶ


def resolve_route(route):
    """route(区間の指定)から、地図に描く線(path)と駅の印(stations)を作る。
    旧形式(駅を [駅名, 緯度, 経度, true] で並べたリスト)もそのまま使える。"""
    if isinstance(route, list):
        st = [[r[0], r[1], r[2], 1 if len(r) > 3 and r[3] else 0] for r in route]
        return [[r[1], r[2]] for r in st], st, []
    region = rail().get(route.get("region", "kanto"))
    if not region:
        sys.exit(f"route.region が不正です: {route.get('region')}(kanto か kansai)")
    path, stations = [], []
    for sg in route.get("segments", []):
        line = region["lines"].get(sg["line"])
        if not line:
            sys.exit(f"路線が見つかりません: {sg['line']}")
        names = [s[0] for s in line["st"]]
        for key in ("from", "to"):
            if sg[key] not in names:
                sys.exit(f"{sg['line']} に駅「{sg[key]}」がありません")
        a = line["st"][names.index(sg["from"])]
        b = line["st"][names.index(sg["to"])]
        pts = trace_segment(line, a[1:], b[1:])
        on = []                    # この区間の線上にある駅を、線に沿った順に拾う
        for name, lon, lat in line["st"]:
            i, d = nearest(pts, [lon, lat])
            if d < 0.35:
                on.append((i, name, lon, lat))
        on.sort()
        for _, name, lon, lat in on:
            major = 1 if name in (sg["from"], sg["to"]) else 0
            if stations and stations[-1][0] == name:
                stations[-1][3] = max(stations[-1][3], major)
            else:
                stations.append([name, lat, lon, major])
        path += [[y, x] for x, y in pts]
    for r in route.get("extra", []):   # 路線データにない駅は、座標を直接指定して直線でつなぐ
        stations.append([r[0], r[1], r[2], 1 if len(r) > 3 and r[3] else 0])
        path.append([r[1], r[2]])
    if len(path) < 2:
        sys.exit("route に区間がありません")
    stations[0][3] = stations[-1][3] = 1
    return path, stations, region["lines"]


def network(lines, views):
    """地図の範囲に入る路線を、背景用に間引いて返す"""
    out = []
    for v in views:
        cs = math.cos(math.radians(v["latc"]))
        v["_lat"] = (v["lon1"] - v["lon0"]) * cs * 0.6
    for line in lines.values():
        for seg in line["segs"]:
            if not any(v["lon0"] - 0.1 < x < v["lon1"] + 0.1 and abs(y - v["latc"]) < v["_lat"] for x, y in seg[::5] for v in views):
                continue
            thin = [seg[0]]
            for pt in seg[1:]:
                if km(thin[-1], pt) > 0.25:
                    thin.append(pt)
            thin.append(seg[-1])
            out.append([line["color"] or "#8f97b8", thin])
    for v in views:
        del v["_lat"]
    return out


# ---------- 駅データ → 動画用データ ----------
REQUIRED = ["station", "kana", "pref", "pref_code", "city", "line", "origin", "km", "dep", "arr", "first",
            "hotel", "netcafe", "bath", "karaoke", "famires", "conveni", "temp_jan", "temp_aug", "route"]


def build(st):
    miss = [k for k in REQUIRED if st.get(k) in (None, "")]
    tt0 = st.get("taxi_to") or {}
    if tt0.get("yen") is None and st.get("taxi_origin_yen") is None:
        miss.append("taxi_to.yen")
    if miss:
        sys.exit("設定ファイルに未入力の項目があります: " + ", ".join(miss))
    st["taxi"] = [t for t in st.get("taxi", []) if t.get("yen") is not None]
    if (st.get("hook_style") or "bakusui") not in ("bakusui", "yokone", "utatane", "headphone", "yopparai", "yuka", "taiiku"):
        sys.exit("hook_style は bakusui / yokone / utatane / headphone / yopparai / yuka / taiiku のどれかにしてください")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", st.get("sign_color") or "#2f8f4e"):
        sys.exit("sign_color は #2f8f4e のような6桁の色コードで指定してください")
    S, O = st["station"], st["origin"]
    dep, arr, first = hm(st["dep"]), hm(st["arr"]), hm(st["first"])
    if arr < dep:
        arr += 1440
    while first <= arr:
        first += 1440
    wait = first - arr
    wait_l = f"約{wait // 60}時間" + (f"{wait % 60}分" if wait % 60 else "")

    # タクシー代の基準駅。taxi_to がなければ始発駅(旧 taxi_origin_yen)を使う
    tt = st.get("taxi_to") or {"name": O, "km": st["km"], "yen": st["taxi_origin_yen"]}
    stay = st["hotel"] + st["netcafe"] + st["bath"]
    kill = st["karaoke"] + st["famires"]
    scores = [score_wait(wait), score_taxi(tt["yen"]), score_stay(stay),
              score_count(kill), score_count(st["conveni"]), score_temp(st["temp_jan"])]
    total = sum(s * w for s, w in zip(scores, WEIGHTS))
    ver = verdict(total)
    P = [f"{s}点" for s in scores]

    taxi_rows = "".join(f"{t['name']}まで{num(t['km'])}キロで{yen(t['yen'])}。" for t in st["taxi"])
    feature = f"{S}は{st['town_feature']}で、" if st.get("town_feature") else ""
    temp_head = ("そして、すべてがダメだったとき、外で待てるのか。気温は、命に関わる真冬、1月の最低気温をスコア化します。"
                 "参考値として、真夏の8月の最低気温も掲載します。"
                 f"{feature}1月の最低気温は平均{deg(st['temp_jan'])}。{stop(st.get('winter_comment', ''))}")
    temp_text = temp_head + f"一方、8月は{deg(st['temp_aug'])}前後{stop(st.get('summer_comment', ''))}この項目は{P[5]}でした。"

    scenes = [
        {"k": "hook", "name": "フック", "pre": 3.4, "gap": 1.1, "end": 2.2,
         "t": f"お客さん、終点ですよ。その声で目を開けると、そこは{st['pref']}、{S}駅でした。"},
        {"k": "hello", "name": "あいさつ",
         "t": f"皆さんこんにちは、{CHANNEL}です。このチャンネルでは、地図データを面白く可視化してお届けしています。"},
        {"k": "intro", "name": "導入",
         "t": f"{st['line']}の終電で寝過ごすと、たどり着くことがある駅、{S}。始発の{O}駅からおよそ{num(st['km'])}キロ。"
              "今回は、この駅に終電で着いてしまった場合の「詰み度」を100点満点で採点します。見るのは4つ。いつ帰れるのか。"
              "いくらで帰れるのか。朝まで過ごせる場所はあるのか。そして、すべてがダメだったとき、外で待てるのか。"
              "この4つを6項目に分けます。「いつ帰れるか」は、始発までの待ち時間。「いくらで帰れるか」は、タクシー代。"
              "「朝まで過ごせる場所」は、泊まれる場所、時間を潰せる場所、コンビニの3項目。「外で待てるか」は、気温です。"},
        {"k": "rule", "name": "採点基準",
         "t": "採点は6項目。それぞれ1点から5点で、点が高いほど詰みです。"
              "始発までの待ち時間は、3時間30分未満なら1点、そこから15分延びるごとに1点上がり、4時間15分以上で5点。"
              f"タクシー代は、{tt['name']}駅まで5千円未満なら1点、3万円以上で5点。"
              "泊まれる場所、時間を潰せる場所、コンビニは、どれも4軒以上あれば1点、1軒減るごとに1点上がり、ゼロなら5点。"
              "気温は、冬で採点します。夏の夜は暑くても朝を待てますが、冬の夜に屋外で過ごすのは命に関わるからです。1月の最低気温が3度以上なら1点、そこから1度下がるごとに1点上がり、0度未満で5点です。"
              "帰れない時間と逃げ場のなさを重く見て項目ごとに重みをかけ、合計100点満点で採点します。"},
        {"k": "wait", "name": "① 始発までの待ち時間", "h": "① 始発までの待ち時間",
         "t": f"まず、何時間耐えればいいのか。そもそも、{O}駅を何時に出る電車が{S}までたどり着いてしまうのでしょうか。"
              f"答えは{jt(st['dep'])}発。{S}駅への到着は{jt(st['arr'])}です。上りの始発は{jt(st['first'])}。"
              f"待ち時間は{wait_l}。詰みの基本は、耐える時間の長さです。この項目は{P[0]}でした。"},
        {"k": "taxi", "name": f"② {tt['name']}駅までのタクシー代", "h": f"② {tt['name']}駅までのタクシー代",
         "t": "次に、お金で解決できるのか。数千円の出費なら、タクシーも選択肢になるでしょう。主な駅までの深夜料金はご覧の通りです。"
              f"{taxi_rows}{'そして' if st['taxi'] else ''}{tt['name']}駅までは{yen(tt['yen'])}。"
              f"{stop(st.get('taxi_comment', ''))}この項目は{P[1]}でした。"},
        {"k": "cnt", "name": "③ 泊まれる場所", "h": "③ 泊まれる場所", "sub": "駅から500m以内",
         "note": "※ ホテルはビジネスホテルに限定して集計",
         "items": [item(st, "ビジネスホテル", "hotel", "軒", 2), item(st, "ネットカフェ", "netcafe", "軒", 3),
                   item(st, "深夜営業の入浴施設", "bath", "軒", 4)],
         "t": "では、朝まで横になれる場所はあるのか。なお、ホテルはビジネスホテルに限定して数えます。"
              f"駅から500メートル以内のビジネスホテルは、{shops(st, 'hotel', '軒')}。ネットカフェは、{shops(st, 'netcafe', '軒')}。"
              f"深夜まで開いている入浴施設は、{shops(st, 'bath', '軒')}。横になれるかどうかで、翌日のダメージは大きく変わります。"
              f"ただし、深夜の飛び込みで部屋が取れるかは運次第です。この項目は{P[2]}でした。"},
        {"k": "cnt", "name": "④ 時間を潰せる場所", "h": "④ 時間を潰せる場所", "sub": "屋根と椅子",
         "items": [item(st, "カラオケ", "karaoke", "軒", 1), item(st, "24時間ファミレス", "famires", "軒", 2)],
         "t": f"泊まれなくても、屋根と椅子があれば朝まで持ちます。カラオケは、{shops(st, 'karaoke', '軒')}。"
              f"24時間営業のファミレスは、{shops(st, 'famires', '軒')}。この項目は{P[3]}でした。"},
        {"k": "cnt", "name": "⑤ 24時間コンビニ", "h": "⑤ 24時間コンビニ", "sub": "最後の命綱",
         "items": [item(st, "24時間営業", "conveni", "店", 1)]
                  + ([["最寄りまで徒歩", st["conveni_walk_min"], "分", 2]] if st["conveni"] else []),
         "t": f"最後の命綱がコンビニです。24時間営業の店は、{shops(st, 'conveni', '店')}。"
              + (f"一番近い店は駅から徒歩{st['conveni_walk_min']}分ほど。温かい飲み物と充電とトイレは、ここで確保できます。"
                 if st["conveni"] else "温かい飲み物も充電もトイレも、ここでは確保できません。")
              + f"この項目は{P[4]}でした。"},
        {"k": "temp", "name": "⑥ 気温", "h": "⑥ 気温", "sub": "外で待てるのか",
         "cue": {"sum": temp_head.count("。")}, "t": temp_text},
        {"k": "result", "name": "結果発表",
         "t": f"6項目が出そろいました。{S}駅の詰み度は……{total}点。判定は「{ver}」。{stop(st.get('review', ''))}"},
        {"k": "outro", "name": "締め",
         "t": "採点基準は固定コメントに載せています。あなたの路線の終点は何点でしょうか。"
              "採点してほしい駅があれば、ぜひコメントで教えてください。"
              "この動画が面白かったら、高評価とチャンネル登録をよろしくお願いします。それでは、寝過ごしにはお気をつけて。"},
    ]

    src = st.get("sources", {})
    temp_src = src.get("temp", "気象庁")
    if st.get("temp_station"):
        temp_src += f"({st['temp_station']}観測点)"
    path, route, lines = resolve_route(st["route"])
    pts = [tuple(p) for p in path]
    o, e = pts[0], pts[-1]
    view_intro = map_view(pts, 1120, 262, 0.3, 0.84)
    view_hook = map_view([o, e], 1020, 690, 2.2, 0.45,
                         center=(e[0] * 0.6 + o[0] * 0.4, e[1] * 0.6 + o[1] * 0.4))
    data = {
        "station": S, "kana": st["kana"], "pref": st["pref"], "prefCode": st["pref_code"], "city": st["city"],
        "line": st["line"], "origin": O, "km": st["km"], "channel": CHANNEL, "tagline": TAGLINE,
        "cps": st.get("chars_per_sec", 6.2),
        "hookStyle": st.get("hook_style") or "bakusui",
        "signColor": st.get("sign_color") or "#2f8f4e",
        "dep": dep / 60, "arr": arr / 60, "first": first / 60,
        "depL": st["dep"], "arrL": st["arr"], "firstL": st["first"], "waitL": wait_l,
        "taxi": [[t["name"], f"{num(t['km'])}km", t["yen"]] for t in st["taxi"]]
                + [[tt["name"], f"{num(tt['km'])}km", tt["yen"]]],
        "taxiTo": tt["name"],
        "win": st["temp_jan"], "sum": st["temp_aug"],
        "src": {"time": src.get("timetable", "ナビタイム"), "taxi": src.get("taxi", "ナビタイム"),
                "shop": src.get("shop", "Googleマップ"), "temp": temp_src},
        "scores": scores, "total": total, "verdict": ver, "route": route,
        "path": path, "net": network(lines, [view_intro, view_hook]),
        "mapIntro": view_intro, "mapHook": view_hook,
    }
    return data, scenes


def sentences(sc):
    return [s for s in re.split(r"(?<=。)", sc["t"]) if s]


# ---------- ナレーション ----------
RATE = 48000


def audio_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def voicevox(text, path, speaker, url, speed):
    """VOICEVOXエンジン(起動しておく)で1文を合成する。同じ文・同じ設定なら作り直さない"""
    tag = hashlib.sha1(f"{speaker}|{speed}|{text}".encode()).hexdigest()
    mark = path.with_suffix(".tag")
    if path.exists() and mark.exists() and mark.read_text() == tag:
        return
    q = urllib.request.Request(f"{url}/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker}), method="POST")
    query = json.loads(urllib.request.urlopen(q).read())
    query["speedScale"] = speed
    r = urllib.request.Request(f"{url}/synthesis?speaker={speaker}", data=json.dumps(query).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    path.write_bytes(urllib.request.urlopen(r).read())
    mark.write_text(tag)


def voice_files(scenes, a, voice_dir):
    """1文ごとの音声ファイルを用意して、文の順に返す"""
    files, n = [], 0
    for sc in scenes:
        for text in sentences(sc):
            n += 1
            if a.voicevox is not None:
                f = voice_dir / f"{n:03}.wav"
                try:
                    voicevox(text, f, a.voicevox, a.voicevox_url.rstrip("/"), a.voice_speed)
                except OSError as e:
                    sys.exit(f"VOICEVOXに接続できません({a.voicevox_url})。VOICEVOXを起動してから実行してください: {e}")
            else:
                hit = sorted(voice_dir.glob(f"{n:03}.*"))
                hit = [h for h in hit if h.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")]
                if not hit:
                    sys.exit(f"{voice_dir} に {n:03} 番の音声がありません(文: {text})")
                f = hit[0]
            files.append(f)
    return files


def mix_narration(files, cues, total, out_path):
    """各文の音声を字幕の開始時刻に並べて、1本のWAVにする"""
    buf = bytearray(int(total * RATE) * 2 + 2)
    for f, (start, _, _) in zip(files, cues):
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
                             capture_output=True, check=True).stdout
        at = int(start * RATE) * 2
        buf[at:at + len(pcm)] = pcm[:max(0, len(buf) - at)]
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(buf))


def timeline(data, scenes):
    """字幕1文ごとの開始・終了秒(template.html と同じ計算)。sc["d"] があれば音声の長さを使う"""
    out, t0 = [], 0.0
    for sc in scenes:
        sents = sentences(sc)
        a, starts = 0.4 + sc.get("pre", 0), []
        for i, s in enumerate(sents):
            starts.append(a)
            a += (sc["d"][i] if "d" in sc else len(s) / data["cps"]) + 0.3 + (sc.get("gap", 0) if i == 0 else 0)
        dur = a + 0.5 + sc.get("end", 0)
        for i, s in enumerate(sents):
            end = starts[i + 1] if i + 1 < len(sents) else dur
            out.append((t0 + starts[i], t0 + end, s))
        t0 += dur
    return out, t0


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def make_html(data, scenes, fragment=False):
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    pref = (HERE / "pref.json").read_text(encoding="utf-8")
    js = lambda o: json.dumps(o, ensure_ascii=False)
    for key, val in (("/*__D__*/null", js(data)), ("/*__SC__*/null", js(scenes)), ("/*__PREF__*/null", pref)):
        if key not in tpl:
            sys.exit(f"template.html に {key} がありません")
        tpl = tpl.replace(key, val)
    if fragment:
        return tpl
    return '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0">' + tpl + "</body></html>"


def render_video(html_path, mp4_path, fps, audio):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(html_path.resolve().as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(1500)       # Webフォントの読み込み待ち
        total = page.evaluate("TOTAL")
        frames = int(total * fps) + 1
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-"]
        if audio:
            cmd += ["-i", str(audio)]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(mp4_path)]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        grab = "t=>{T=t;render();return document.getElementById('cv').toDataURL('image/jpeg',0.93).split(',')[1]}"
        for i in range(frames):
            ff.stdin.write(base64.b64decode(page.evaluate(grab, min(i / fps, total))))
            if i % (fps * 5) == 0:
                print(f"\r  {i / frames:5.0%}  {i}/{frames} コマ", end="", flush=True)
        ff.stdin.close()
        if ff.wait() != 0:
            sys.exit("ffmpeg が失敗しました")
        browser.close()
    print(f"\r  100%  {frames}/{frames} コマ")


def main():
    ap = argparse.ArgumentParser(description="終電寝過ごし「詰み度」動画ジェネレーター")
    ap.add_argument("stations", nargs="+", help="駅の設定ファイル(JSON)。複数指定可")
    ap.add_argument("--out", default="out", help="出力フォルダ(既定: out)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--audio", help="1本の音声ファイルをそのまま重ねる(タイミングは合わせない)")
    ap.add_argument("--voicevox", type=int, metavar="話者ID", help="VOICEVOXで読み上げを合成し、声の長さに動画を合わせる")
    ap.add_argument("--voicevox-url", default="http://127.0.0.1:50021")
    ap.add_argument("--voice-speed", type=float, default=1.0, help="VOICEVOXの話速(既定 1.0)")
    ap.add_argument("--voice-dir", help="収録した音声のフォルダ。001.wav, 002.wav … を文の順に置く(番号は 駅名_lines.txt を参照)")
    ap.add_argument("--no-video", action="store_true", help="原稿・字幕・確認用HTMLだけ作る")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for path in a.stations:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
        name = Path(path).stem
        data, scenes = build(st)
        lines = [t for sc in scenes for t in sentences(sc)]
        (out / f"{name}_lines.txt").write_text(
            "".join(f"{i:03}\t{t}\n" for i, t in enumerate(lines, 1)), encoding="utf-8")
        audio = a.audio
        if a.voicevox is not None or a.voice_dir:
            vdir = Path(a.voice_dir) if a.voice_dir else out / f"{name}_voice"
            vdir.mkdir(parents=True, exist_ok=True)
            files = voice_files(scenes, a, vdir)
            durs = iter(audio_seconds(f) for f in files)
            for sc in scenes:                      # 声の長さを各文の表示時間にする
                sc["d"] = [round(next(durs), 3) for _ in sentences(sc)]
        cues, total = timeline(data, scenes)
        if a.voicevox is not None or a.voice_dir:
            audio = out / f"{name}_narration.wav"
            mix_narration(files, cues, total, audio)
        print(f"{st['station']}: {data['total']}点「{data['verdict']}」  "
              + " ".join(f"{n}{s}" for n, s in zip(AXES, data["scores"]))
              + f"  尺 {int(total // 60)}分{int(total % 60):02}秒")
        src = data["src"]
        (out / f"{name}.txt").write_text(
            "\n\n".join(s["t"] for s in scenes) + "\n", encoding="utf-8")
        (out / f"{name}_sources.txt").write_text(
            f"時刻表：{src['time']}　タクシー料金：{src['taxi']}　店舗：{src['shop']}　気温：{src['temp']}\n",
            encoding="utf-8")
        (out / f"{name}.srt").write_text(
            "".join(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(cues, 1)),
            encoding="utf-8")
        html = out / f"{name}.html"
        html.write_text(make_html(data, scenes), encoding="utf-8")
        if not a.no_video:
            render_video(html, out / f"{name}.mp4", a.fps, audio)
            print(f"  → {out / name}.mp4")


if __name__ == "__main__":
    main()
