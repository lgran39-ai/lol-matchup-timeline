"""
LoL Matchup Timeline Data Collector（差分取得版）
==================================================
前回取得した試合IDを記録し、新しい試合のみ取得して蓄積します。

使い方:
  1. RIOT_API_KEY を自分のキーに置き換える
  2. python collect_matchup_data_v2.py を実行
  3. 実行するたびにデータが蓄積されていく

初回実行: MAX_NEW_MATCHES分の試合を取得
2回目以降: 前回以降の新しい試合のみ取得（高速）
"""

import requests
import json
import time
import os
from collections import defaultdict
from datetime import datetime

# ============================================================
# 設定
# ============================================================

RIOT_API_KEY = "RGAPI-5789d316-6c74-4653-81de-9d31536de353"  # ← ここに自分のキーを貼る

# サーバー設定
PLATFORM = "jp1"                    # jp1, kr, na1, euw1 等
REGION = "asia"                     # asia, americas, europe

# 収集設定
RANKED_SOLO_QUEUE = 420
TARGET_TIERS = ["CHALLENGER", "GRANDMASTER", "MASTER"]
MAX_MATCHES_PER_PLAYER = 20
MAX_PLAYERS = 500
MAX_NEW_MATCHES = 500               # 1回あたりの新規取得上限
MIN_GAME_DURATION = 900             # 15分未満の試合を除外

# ファイルパス
OUTPUT_DIR = "data"
RAW_DIR = os.path.join(OUTPUT_DIR, "raw")
MATCHUP_DIR = os.path.join(OUTPUT_DIR, "matchups")
COLLECTED_IDS_FILE = os.path.join(OUTPUT_DIR, "collected_match_ids.txt")
ALL_MATCHES_FILE = os.path.join(OUTPUT_DIR, "all_matches.json")

# ============================================================
# レートリミット管理
# ============================================================

class RateLimiter:
    def __init__(self, rps=20, rpm=100):
        self.short = []
        self.long = []
        self.rps = rps
        self.rpm = rpm
        self.total = 0

    def wait(self):
        now = time.time()
        self.short = [t for t in self.short if now - t < 1.0]
        self.long = [t for t in self.long if now - t < 120.0]
        if len(self.short) >= self.rps:
            sl = 1.0 - (now - self.short[0]) + 0.05
            if sl > 0: time.sleep(sl)
        if len(self.long) >= self.rpm:
            sl = 120.0 - (now - self.long[0]) + 0.1
            if sl > 0:
                print(f"  ⏳ レートリミット待機: {sl:.1f}秒...")
                time.sleep(sl)
        self.short.append(time.time())
        self.long.append(time.time())
        self.total += 1

rl = RateLimiter()

def api_req(url, retries=3):
    for attempt in range(retries):
        rl.wait()
        try:
            resp = requests.get(url, headers={"X-Riot-Token": RIOT_API_KEY})
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                ra = int(resp.headers.get("Retry-After", 10))
                print(f"  ⚠️ 429 Rate Limited. {ra}秒待機...")
                time.sleep(ra + 1)
                continue
            elif resp.status_code == 403:
                print("  ❌ 403 Forbidden - APIキーが無効か失効しています")
                return None
            elif resp.status_code == 404:
                return None
            else:
                print(f"  ⚠️ HTTP {resp.status_code}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue
        except requests.exceptions.RequestException as e:
            print(f"  ⚠️ エラー: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            continue
    return None

# ============================================================
# 取得済みID管理
# ============================================================

def load_collected_ids():
    """取得済みの試合IDをファイルから読み込む"""
    if not os.path.exists(COLLECTED_IDS_FILE):
        return set()
    with open(COLLECTED_IDS_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_collected_ids(ids):
    """取得済みの試合IDをファイルに保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(COLLECTED_IDS_FILE, "w") as f:
        for mid in sorted(ids):
            f.write(mid + "\n")

# ============================================================
# 蓄積済み試合データ管理
# ============================================================

def load_all_matches():
    """蓄積済みの全試合データを読み込む"""
    if not os.path.exists(ALL_MATCHES_FILE):
        return []
    try:
        with open(ALL_MATCHES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_all_matches(matches):
    """全試合データを保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(ALL_MATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=True)

# ============================================================
# Step 1: プレイヤー取得
# ============================================================

def get_puuids():
    print("=" * 60)
    print("Step 1: 高ランクプレイヤー取得")
    print("=" * 60)
    puuids = []
    for tier in TARGET_TIERS:
        print(f"\n📊 {tier} を取得中...")
        url = f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/{tier.lower()}leagues/by-queue/RANKED_SOLO_5x5"
        data = api_req(url)
        if not data or "entries" not in data:
            print(f"  ❌ {tier} 取得失敗")
            continue
        entries = data["entries"]
        print(f"  ✅ {len(entries)} プレイヤー")
        for e in entries[:MAX_PLAYERS]:
            if "puuid" in e:
                puuids.append(e["puuid"])
        if len(puuids) >= MAX_PLAYERS:
            break
    print(f"\n📋 {len(puuids)} プレイヤー取得")
    return puuids

# ============================================================
# Step 2: 新しい試合IDのみ取得
# ============================================================

def get_new_match_ids(puuids, collected_ids):
    print("\n" + "=" * 60)
    print("Step 2: 新しい試合IDを取得")
    print(f"  取得済み: {len(collected_ids)} 試合")
    print("=" * 60)

    new_ids = []
    checked = 0

    for i, puuid in enumerate(puuids):
        if len(new_ids) >= MAX_NEW_MATCHES:
            break

        url = (
            f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?queue={RANKED_SOLO_QUEUE}&type=ranked&count={MAX_MATCHES_PER_PLAYER}"
        )
        ids = api_req(url)
        if not ids:
            continue

        for mid in ids:
            if mid not in collected_ids and mid not in [x for x in new_ids]:
                new_ids.append(mid)

        checked += 1
        if checked % 20 == 0:
            print(f"  [{checked}/{len(puuids)}] 新規: {len(new_ids)} 試合")

        if len(new_ids) >= MAX_NEW_MATCHES:
            print(f"\n  🎯 上限 {MAX_NEW_MATCHES} に到達")
            break

    print(f"\n📋 新規 {len(new_ids)} 試合を発見（スキップ: 既に取得済み）")
    return new_ids

# ============================================================
# Step 3: 新しい試合の詳細+タイムライン取得
# ============================================================

def get_match_data(match_ids):
    print("\n" + "=" * 60)
    print(f"Step 3: {len(match_ids)} 試合の詳細+タイムライン取得")
    print("=" * 60)

    matches = []
    for i, match_id in enumerate(match_ids):
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(match_ids)}] 取得中...")

        url_match = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        match_detail = api_req(url_match)
        if not match_detail:
            continue

        info = match_detail.get("info", {})
        duration = info.get("gameDuration", 0)
        if duration < MIN_GAME_DURATION:
            continue

        url_tl = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        timeline = api_req(url_tl)
        if not timeline:
            continue

        participants = []
        for p in info.get("participants", []):
            participants.append({
                "participantId": p.get("participantId"),
                "championName": p.get("championName"),
                "teamPosition": p.get("teamPosition"),
                "win": p.get("win"),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "goldEarned": p.get("goldEarned"),
                "totalMinionsKilled": p.get("totalMinionsKilled"),
                "neutralMinionsKilled": p.get("neutralMinionsKilled"),
            })

        frames = []
        for frame in timeline.get("info", {}).get("frames", []):
            ts = frame.get("timestamp", 0) // 60000
            pfs = {}
            for pid_str, pf in frame.get("participantFrames", {}).items():
                pfs[int(pid_str)] = {
                    "totalGold": pf.get("totalGold", 0),
                    "xp": pf.get("xp", 0),
                    "minionsKilled": pf.get("minionsKilled", 0),
                    "jungleMinionsKilled": pf.get("jungleMinionsKilled", 0),
                    "level": pf.get("level", 1),
                }
            frames.append({"minute": ts, "participants": pfs})

        matches.append({
            "matchId": match_id,
            "duration": duration,
            "patch": info.get("gameVersion", ""),
            "participants": participants,
            "frames": frames,
        })

    print(f"\n📋 {len(matches)} 試合を取得")
    return matches

# ============================================================
# Step 4: マッチアップ集計（全蓄積データから）
# ============================================================

ROLE_MAP = {"TOP": "top", "JUNGLE": "jg", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "sup"}
TIME_BUCKETS = [
    ("0-5min", "Lv1-3", 0, 5),
    ("5-10min", "Lv4-6", 5, 10),
    ("10-15min", "Lv7-9", 10, 15),
    ("15-25min", "mid_game", 15, 25),
    ("25min+", "late_game", 25, 99),
]

def aggregate_matchups(all_matches):
    print("\n" + "=" * 60)
    print(f"Step 4: {len(all_matches)} 試合からマッチアップ集計")
    print("=" * 60)

    matchup_data = defaultdict(lambda: {
        "gold_diffs": defaultdict(list),
        "xp_diffs": defaultdict(list),
        "cs_diffs": defaultdict(list),
        "wins_a": 0,
        "total": 0,
    })

    for match in all_matches:
        participants = match["participants"]
        frames = match["frames"]
        role_players = defaultdict(list)
        for p in participants:
            pos = p.get("teamPosition", "")
            if pos in ROLE_MAP:
                role_players[pos].append(p)

        for position, players in role_players.items():
            if len(players) != 2:
                continue
            role = ROLE_MAP[position]
            p1, p2 = players[0], players[1]
            if p1["championName"] > p2["championName"]:
                p1, p2 = p2, p1

            champ_a = p1["championName"]
            champ_b = p2["championName"]
            key = (champ_a, champ_b, role)
            pid_a = p1["participantId"]
            pid_b = p2["participantId"]

            matchup_data[key]["total"] += 1
            if p1.get("win"):
                matchup_data[key]["wins_a"] += 1

            for frame in frames:
                minute = frame["minute"]
                pframes = frame["participants"]
                if pid_a not in pframes or pid_b not in pframes:
                    continue
                fa = pframes[pid_a]
                fb = pframes[pid_b]
                gold_diff = fa["totalGold"] - fb["totalGold"]
                xp_diff = fa["xp"] - fb["xp"]
                cs_a = fa["minionsKilled"] + fa["jungleMinionsKilled"]
                cs_b = fb["minionsKilled"] + fb["jungleMinionsKilled"]
                cs_diff = cs_a - cs_b

                for bucket_id, _, start, end in TIME_BUCKETS:
                    if start <= minute < end:
                        matchup_data[key]["gold_diffs"][bucket_id].append(gold_diff)
                        matchup_data[key]["xp_diffs"][bucket_id].append(xp_diff)
                        matchup_data[key]["cs_diffs"][bucket_id].append(cs_diff)
                        break

    # JSON出力
    os.makedirs(MATCHUP_DIR, exist_ok=True)
    count = 0

    for (champ_a, champ_b, role), data in sorted(matchup_data.items()):
        if data["total"] < 1:
            continue

        timeline = []
        for bucket_id, label, _, _ in TIME_BUCKETS:
            gd = data["gold_diffs"].get(bucket_id, [])
            xd = data["xp_diffs"].get(bucket_id, [])
            cd = data["cs_diffs"].get(bucket_id, [])
            if not gd:
                continue

            avg_g = sum(gd) / len(gd)
            avg_x = sum(xd) / len(xd) if xd else 0
            avg_c = sum(cd) / len(cd) if cd else 0

            if avg_g > 400: adv = 2
            elif avg_g > 150: adv = 1
            elif avg_g > -150: adv = 0
            elif avg_g > -400: adv = -1
            else: adv = -2

            timeline.append({
                "phase": bucket_id,
                "label": label,
                "avg_gold_diff": round(avg_g),
                "avg_xp_diff": round(avg_x),
                "avg_cs_diff": round(avg_c, 1),
                "sample_frames": len(gd),
                "advantage": adv,
            })

        if not timeline:
            continue

        wr = (data["wins_a"] / data["total"] * 100) if data["total"] > 0 else 50.0

        matchup_json = {
            "champA": champ_a,
            "champB": champ_b,
            "role": role,
            "sample_size": data["total"],
            "overall_winrate_a": round(wr, 1),
            "updated_at": datetime.now().isoformat(),
            "timeline": timeline,
        }

        role_dir = os.path.join(MATCHUP_DIR, role)
        os.makedirs(role_dir, exist_ok=True)
        filepath = os.path.join(role_dir, f"{champ_a}_vs_{champ_b}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(matchup_json, f, ensure_ascii=True, indent=2)
        count += 1

    print(f"\n📋 {count} マッチアップを集計・保存")
    return count

# ============================================================
# メイン
# ============================================================

def main():
    print("🎮 LoL Matchup Data Collector v2（差分取得版）")
    print(f"📡 サーバー: {PLATFORM} ({REGION})")
    print(f"🎯 対象: {', '.join(TARGET_TIERS)}")
    print(f"📊 1回あたりの新規取得上限: {MAX_NEW_MATCHES}")
    print()

    if RIOT_API_KEY == "YOUR_API_KEY_HERE":
        print("❌ RIOT_API_KEY を設定してください！")
        return

    start = time.time()

    # 取得済みIDを読み込む
    collected_ids = load_collected_ids()
    print(f"📂 取得済み: {len(collected_ids)} 試合")

    # 蓄積済み試合データを読み込む
    all_matches = load_all_matches()
    print(f"📂 蓄積済み: {len(all_matches)} 試合データ")

    # Step 1
    puuids = get_puuids()
    if not puuids:
        print("❌ プレイヤーが見つかりません")
        return

    # Step 2: 新しい試合のみ取得
    new_ids = get_new_match_ids(puuids, collected_ids)
    if not new_ids:
        print("\n✅ 新しい試合はありません。集計だけ更新します。")
        aggregate_matchups(all_matches)
        elapsed = time.time() - start
        print(f"\n⏱️ 所要時間: {elapsed/60:.1f}分")
        return

    # Step 3: 新しい試合の詳細取得
    new_matches = get_match_data(new_ids)

    # 取得済みIDを更新
    for m in new_matches:
        collected_ids.add(m["matchId"])
    save_collected_ids(collected_ids)

    # 蓄積データに追加
    all_matches.extend(new_matches)
    save_all_matches(all_matches)

    # Step 4: 全データから集計
    matchup_count = aggregate_matchups(all_matches)

    # 完了
    elapsed = time.time() - start
    print("\n" + "=" * 60)
    print("✅ 完了！")
    print("=" * 60)
    print(f"⏱️  所要時間: {elapsed/60:.1f}分")
    print(f"📡 APIリクエスト数: {rl.total}")
    print(f"🆕 新規取得: {len(new_matches)} 試合")
    print(f"📊 蓄積合計: {len(all_matches)} 試合")
    print(f"📋 マッチアップ数: {matchup_count}")
    print(f"\n💾 ファイル:")
    print(f"   取得済みID: {COLLECTED_IDS_FILE} ({len(collected_ids)} IDs)")
    print(f"   全試合データ: {ALL_MATCHES_FILE}")
    print(f"   マッチアップ: {MATCHUP_DIR}/")

if __name__ == "__main__":
    main()
