"""
LoL Matchup Timeline Data Collector
====================================
Riot APIから試合データを収集し、マッチアップごとの
時間帯別ゴールド差を集計するスクリプト。

使い方:
  1. RIOT_API_KEY を自分のキーに置き換える
  2. python collect_matchup_data.py を実行
  3. data/ フォルダにJSONが出力される

注意:
  - 開発キーは24時間で失効します
  - レートリミット: 20 req/秒, 100 req/2分
  - このスクリプトは自動でレートリミットを管理します
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

# 日本サーバー
PLATFORM = "kr"                    # プラットフォーム (jp1, kr, na1, euw1 等)
REGION = "asia"                     # リージョン (asia, americas, europe)

# 収集設定
RANKED_SOLO_QUEUE = 420             # ランクソロキューのID
TARGET_TIERS = ["CHALLENGER", "GRANDMASTER", "MASTER"]
MAX_MATCHES_PER_PLAYER = 20        # 1プレイヤーあたりの取得試合数
MAX_PLAYERS = 500                    # 取得するプレイヤー数（テスト用に少なめ）
MAX_MATCHES_TOTAL = 4000             # 合計取得試合数の上限（テスト用）
MIN_GAME_DURATION = 900             # 最低試合時間（秒）= 15分

# 出力先
OUTPUT_DIR = "data"
RAW_DIR = os.path.join(OUTPUT_DIR, "raw")
MATCHUP_DIR = os.path.join(OUTPUT_DIR, "matchups")

# ============================================================
# レートリミット管理
# ============================================================

class RateLimiter:
    """Riot APIのレートリミットを管理"""
    def __init__(self, requests_per_second=20, requests_per_2min=100):
        self.short_window = []   # 1秒間のリクエスト記録
        self.long_window = []    # 2分間のリクエスト記録
        self.rps = requests_per_second
        self.rpm = requests_per_2min
        self.total_requests = 0

    def wait_if_needed(self):
        now = time.time()

        # 1秒ウィンドウのクリーンアップ
        self.short_window = [t for t in self.short_window if now - t < 1.0]
        # 2分ウィンドウのクリーンアップ
        self.long_window = [t for t in self.long_window if now - t < 120.0]

        # 1秒リミットチェック
        if len(self.short_window) >= self.rps:
            sleep_time = 1.0 - (now - self.short_window[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)

        # 2分リミットチェック
        if len(self.long_window) >= self.rpm:
            sleep_time = 120.0 - (now - self.long_window[0]) + 0.1
            if sleep_time > 0:
                print(f"  ⏳ レートリミット待機: {sleep_time:.1f}秒...")
                time.sleep(sleep_time)

        self.short_window.append(time.time())
        self.long_window.append(time.time())
        self.total_requests += 1


rate_limiter = RateLimiter()

# ============================================================
# API呼び出し
# ============================================================

def api_request(url, retries=3):
    """レートリミット付きAPIリクエスト"""
    for attempt in range(retries):
        rate_limiter.wait_if_needed()
        try:
            resp = requests.get(url, headers={"X-Riot-Token": RIOT_API_KEY})

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                # レートリミット超過 → Retry-After ヘッダーに従う
                retry_after = int(resp.headers.get("Retry-After", 10))
                print(f"  ⚠️ 429 Rate Limited. {retry_after}秒待機...")
                time.sleep(retry_after + 1)
                continue
            elif resp.status_code == 403:
                print("  ❌ 403 Forbidden - APIキーが無効か失効しています")
                print("     → Developer Portal でキーを再生成してください")
                return None
            elif resp.status_code == 404:
                return None
            else:
                print(f"  ⚠️ HTTP {resp.status_code}: {url[:80]}...")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
        except requests.exceptions.RequestException as e:
            print(f"  ⚠️ リクエストエラー: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
    return None

# ============================================================
# Step 1: 高ランクプレイヤーのPUUID取得
# ============================================================

def get_high_elo_puuids():
    """チャレンジャー〜マスターのプレイヤーPUUIDを取得"""
    print("=" * 60)
    print("Step 1: 高ランクプレイヤーのPUUID取得")
    print("=" * 60)

    puuids = []

    for tier in TARGET_TIERS:
        print(f"\n📊 {tier} リーグを取得中...")
        url = f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/{tier.lower()}leagues/by-queue/RANKED_SOLO_5x5"
        data = api_request(url)

        if not data or "entries" not in data:
            print(f"  ❌ {tier} のデータ取得に失敗")
            continue

        entries = data["entries"]
        print(f"  ✅ {len(entries)} プレイヤー found")

        for entry in entries[:MAX_PLAYERS]:
            if "puuid" in entry:
                puuids.append({
                    "puuid": entry["puuid"],
                    "tier": tier,
                    "lp": entry.get("leaguePoints", 0),
                    "wins": entry.get("wins", 0),
                    "losses": entry.get("losses", 0),
                })

        if len(puuids) >= MAX_PLAYERS:
            break

    print(f"\n📋 合計 {len(puuids)} プレイヤーのPUUIDを取得")
    return puuids

# ============================================================
# Step 2: 試合IDの取得
# ============================================================

def get_match_ids(puuids):
    """各プレイヤーの直近ランク戦の試合IDを取得"""
    print("\n" + "=" * 60)
    print("Step 2: 試合IDの取得")
    print("=" * 60)

    match_ids = set()

    for i, player in enumerate(puuids):
        puuid = player["puuid"]
        print(f"\n  [{i+1}/{len(puuids)}] {player['tier']} {player['lp']}LP の試合を取得中...")

        url = (
            f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?queue={RANKED_SOLO_QUEUE}&type=ranked&count={MAX_MATCHES_PER_PLAYER}"
        )
        ids = api_request(url)

        if ids:
            new_ids = set(ids) - match_ids
            match_ids.update(new_ids)
            print(f"    ✅ {len(ids)} 試合 ({len(new_ids)} 新規)")
        else:
            print(f"    ❌ 取得失敗")

        if len(match_ids) >= MAX_MATCHES_TOTAL:
            print(f"\n  🎯 上限 {MAX_MATCHES_TOTAL} 試合に到達")
            break

    print(f"\n📋 合計 {len(match_ids)} ユニーク試合IDを取得")
    return list(match_ids)

# ============================================================
# Step 3: 試合詳細 + タイムライン取得
# ============================================================

def get_match_data(match_ids):
    """各試合の詳細とタイムラインを取得"""
    print("\n" + "=" * 60)
    print("Step 3: 試合詳細 + タイムライン取得")
    print("=" * 60)

    matches = []
    os.makedirs(RAW_DIR, exist_ok=True)

    for i, match_id in enumerate(match_ids):
        print(f"\n  [{i+1}/{len(match_ids)}] {match_id}")

        # 試合詳細
        url_match = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        match_detail = api_request(url_match)

        if not match_detail:
            print(f"    ❌ 詳細取得失敗")
            continue

        info = match_detail.get("info", {})

        # 試合時間チェック（リメイク除外）
        duration = info.get("gameDuration", 0)
        if duration < MIN_GAME_DURATION:
            print(f"    ⏭️ スキップ（{duration}秒 < {MIN_GAME_DURATION}秒）")
            continue

        # タイムライン取得
        url_timeline = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        timeline = api_request(url_timeline)

        if not timeline:
            print(f"    ❌ タイムライン取得失敗")
            continue

        # 参加者情報を整理
        participants = []
        for p in info.get("participants", []):
            participants.append({
                "participantId": p.get("participantId"),
                "puuid": p.get("puuid"),
                "championName": p.get("championName"),
                "teamPosition": p.get("teamPosition"),  # TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY
                "win": p.get("win"),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "goldEarned": p.get("goldEarned"),
                "totalMinionsKilled": p.get("totalMinionsKilled"),
                "neutralMinionsKilled": p.get("neutralMinionsKilled"),
            })

        # タイムラインフレームを整理
        frames = []
        for frame in timeline.get("info", {}).get("frames", []):
            timestamp_min = frame.get("timestamp", 0) // 60000
            participant_frames = {}
            for pid_str, pf in frame.get("participantFrames", {}).items():
                participant_frames[int(pid_str)] = {
                    "totalGold": pf.get("totalGold", 0),
                    "xp": pf.get("xp", 0),
                    "minionsKilled": pf.get("minionsKilled", 0),
                    "jungleMinionsKilled": pf.get("jungleMinionsKilled", 0),
                    "level": pf.get("level", 1),
                }
            frames.append({
                "minute": timestamp_min,
                "participants": participant_frames,
            })

        match_data = {
            "matchId": match_id,
            "duration": duration,
            "patch": info.get("gameVersion", ""),
            "participants": participants,
            "frames": frames,
        }
        matches.append(match_data)

        minutes = duration // 60
        champs = [p["championName"] for p in participants[:5]]
        print(f"    ✅ {minutes}分 | {' / '.join(champs[:3])}...")

    # 生データ保存
    raw_path = os.path.join(RAW_DIR, f"matches_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)

    print(f"\n📋 合計 {len(matches)} 試合を取得")
    print(f"💾 保存先: {raw_path}")
    return matches

# ============================================================
# Step 4: マッチアップ集計
# ============================================================

ROLE_MAP = {
    "TOP": "top",
    "JUNGLE": "jg",
    "MIDDLE": "mid",
    "BOTTOM": "adc",
    "UTILITY": "sup",
}

TIME_BUCKETS = [
    ("0-5min",   "Lv1-3",  0,  5),
    ("5-10min",  "Lv4-6",  5, 10),
    ("10-15min", "Lv7-9", 10, 15),
    ("15-25min", "中盤",   15, 25),
    ("25min+",   "終盤",   25, 99),
]

def aggregate_matchups(matches):
    """マッチアップごとに時間帯別のゴールド差を集計"""
    print("\n" + "=" * 60)
    print("Step 4: マッチアップ集計")
    print("=" * 60)

    # 集計用辞書: (champA, champB, role) -> { bucket -> [gold_diffs] }
    matchup_data = defaultdict(lambda: {
        "gold_diffs": defaultdict(list),
        "xp_diffs": defaultdict(list),
        "cs_diffs": defaultdict(list),
        "wins_a": 0,
        "total": 0,
    })

    for match in matches:
        participants = match["participants"]
        frames = match["frames"]

        # ロール別にペアリング（同じポジションの対面を見つける）
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

            # アルファベット順にソート（キーの一貫性のため）
            if p1["championName"] > p2["championName"]:
                p1, p2 = p2, p1

            champ_a = p1["championName"]
            champ_b = p2["championName"]
            key = (champ_a, champ_b, role)

            pid_a = p1["participantId"]
            pid_b = p2["participantId"]

            # 勝敗記録
            matchup_data[key]["total"] += 1
            if p1.get("win"):
                matchup_data[key]["wins_a"] += 1

            # 時間帯別のゴールド差/XP差/CS差を計算
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
    output_count = 0

    for (champ_a, champ_b, role), data in sorted(matchup_data.items()):
        if data["total"] < 1:  # 本番では最低5-10試合にする
            continue

        timeline = []
        for bucket_id, label, _, _ in TIME_BUCKETS:
            gold_diffs = data["gold_diffs"].get(bucket_id, [])
            xp_diffs = data["xp_diffs"].get(bucket_id, [])
            cs_diffs = data["cs_diffs"].get(bucket_id, [])

            if not gold_diffs:
                continue

            avg_gold = sum(gold_diffs) / len(gold_diffs)
            avg_xp = sum(xp_diffs) / len(xp_diffs) if xp_diffs else 0
            avg_cs = sum(cs_diffs) / len(cs_diffs) if cs_diffs else 0

            # advantage スコア: ゴールド差に基づいて -2〜+2
            if avg_gold > 400:
                advantage = 2
            elif avg_gold > 150:
                advantage = 1
            elif avg_gold > -150:
                advantage = 0
            elif avg_gold > -400:
                advantage = -1
            else:
                advantage = -2

            timeline.append({
                "phase": bucket_id,
                "label": label,
                "avg_gold_diff": round(avg_gold),
                "avg_xp_diff": round(avg_xp),
                "avg_cs_diff": round(avg_cs, 1),
                "sample_frames": len(gold_diffs),
                "advantage": advantage,
            })

        if not timeline:
            continue

        winrate_a = (data["wins_a"] / data["total"] * 100) if data["total"] > 0 else 50.0

        matchup_json = {
            "champA": champ_a,
            "champB": champ_b,
            "role": role,
            "sample_size": data["total"],
            "overall_winrate_a": round(winrate_a, 1),
            "updated_at": datetime.now().isoformat(),
            "timeline": timeline,
        }

        # ロール別ディレクトリに保存
        role_dir = os.path.join(MATCHUP_DIR, role)
        os.makedirs(role_dir, exist_ok=True)

        filename = f"{champ_a}_vs_{champ_b}.json"
        filepath = os.path.join(role_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(matchup_json, f, ensure_ascii=False, indent=2)

        output_count += 1

    print(f"\n📋 合計 {output_count} マッチアップを集計・保存")
    print(f"💾 保存先: {MATCHUP_DIR}/")
    return output_count

# ============================================================
# メイン実行
# ============================================================

def main():
    print("🎮 LoL Matchup Timeline Data Collector")
    print(f"📡 サーバー: {PLATFORM} ({REGION})")
    print(f"🎯 対象: {', '.join(TARGET_TIERS)}")
    print(f"📊 最大試合数: {MAX_MATCHES_TOTAL}")
    print()

    if RIOT_API_KEY == "YOUR_API_KEY_HERE":
        print("❌ エラー: RIOT_API_KEY を設定してください！")
        print("   Developer Portal でキーを取得: https://developer.riotgames.com/")
        return

    start_time = time.time()

    # Step 1: プレイヤー取得
    puuids = get_high_elo_puuids()
    if not puuids:
        print("❌ プレイヤーが見つかりません")
        return

    # Step 2: 試合ID取得
    match_ids = get_match_ids(puuids)
    if not match_ids:
        print("❌ 試合が見つかりません")
        return

    # Step 3: 試合データ + タイムライン取得
    matches = get_match_data(match_ids)
    if not matches:
        print("❌ 試合データの取得に失敗")
        return

    # Step 4: マッチアップ集計
    matchup_count = aggregate_matchups(matches)

    # 完了
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("✅ 完了！")
    print("=" * 60)
    print(f"⏱️  所要時間: {elapsed/60:.1f} 分")
    print(f"📡 APIリクエスト数: {rate_limiter.total_requests}")
    print(f"🎮 取得試合数: {len(matches)}")
    print(f"📊 集計マッチアップ数: {matchup_count}")
    print(f"\n💾 出力ファイル:")
    print(f"   生データ: {RAW_DIR}/")
    print(f"   マッチアップ: {MATCHUP_DIR}/")
    print(f"\n🔗 次のステップ:")
    print(f"   1. data/matchups/ の JSON をフロントエンドに接続")
    print(f"   2. MAX_MATCHES_TOTAL を増やしてデータ量を拡大")
    print(f"   3. GitHub Actions に組み込んで自動化")


if __name__ == "__main__":
    main()
