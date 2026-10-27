#!/usr/bin/env python3
"""Auto-download Steam library artwork for non-Steam games.

Reads your non-Steam shortcuts from shortcuts.vdf and fills in the cover,
wide cover, background (hero) and logo in Steam's grid folder.
Sources: the Steam store first, SteamGridDB as fallback.

Works on Windows and Steam Deck / Linux. Uses only the Python standard library.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
USER_AGENT = "Mozilla/5.0 (steam-artwork-script)"
STEAM_ASSET_BASE = "https://shared.steamstatic.com/store_item_assets/"
SGDB_BASE = "https://www.steamgriddb.com/api/v2"
SGDB_KEY_URL = "https://www.steamgriddb.com/profile/preferences/api"
MATCH_THRESHOLD = 0.85  # minimum name similarity to accept a search result

# artwork kind -> grid filename suffix (<id><suffix>.<ext>)
KINDS = {
    "cover": "p",       # portrait 600x900
    "wide": "",         # horizontal 920x430 / 460x215
    "hero": "_hero",    # background 1920x620
    "logo": "_logo",    # transparent logo
    "icon": "_icon",    # small icon; Steam reads its path from shortcuts.vdf, not the grid folder
}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".ico")


# --------------------------------------------------------------------------- HTTP

def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def get_json(url, headers=None):
    data, _ = http_get(url, headers)
    return json.loads(data.decode("utf-8"))


# --------------------------------------------------------------------------- Steam files

def find_steam_path(override=None):
    if override:
        return Path(override).expanduser()
    candidates = []
    if os.name == "nt":
        try:
            import winreg
            keys = ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"))
            for hive, subkey, value in keys:
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        candidates.append(Path(winreg.QueryValueEx(key, value)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
        candidates.append(Path(r"C:\Program Files (x86)\Steam"))
    else:
        home = Path.home()
        candidates += [home / ".local/share/Steam",
                       home / ".steam/steam",
                       home / ".steam/root",
                       home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]  # Flatpak
    for path in candidates:
        if (path / "userdata").is_dir():
            return path
    return None


class UInt64(int):
    """Marks a 64-bit VDF value so it's written back with the same type."""


class Float32(float):
    """Marks a float VDF value so it's written back with the same type."""


def parse_binary_vdf(data):
    """Parse Valve's binary KeyValues format (used by shortcuts.vdf)."""
    pos = 0

    def read_cstr():
        nonlocal pos
        end = data.index(b"\x00", pos)
        text = data[pos:end].decode("utf-8", errors="surrogateescape")  # round-trips odd bytes
        pos = end + 1
        return text

    def read_map():
        nonlocal pos
        result = {}
        while pos < len(data):
            kind = data[pos]
            pos += 1
            if kind == 0x08:  # end of map
                return result
            key = read_cstr()
            if kind == 0x00:
                result[key] = read_map()
            elif kind == 0x01:
                result[key] = read_cstr()
            elif kind == 0x02:
                result[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4
            elif kind == 0x03:
                result[key] = Float32(struct.unpack_from("<f", data, pos)[0])
                pos += 4
            elif kind == 0x07:
                result[key] = UInt64(struct.unpack_from("<Q", data, pos)[0])
                pos += 8
            else:
                raise ValueError(f"Unknown VDF type 0x{kind:02x} at byte {pos - 1}")
        return result

    return read_map()


def dump_binary_vdf(obj):
    out = bytearray()

    def cstr(text):
        return text.encode("utf-8", errors="surrogateescape") + b"\x00"

    def write_map(mapping):
        for key, value in mapping.items():
            if isinstance(value, dict):
                out.extend(b"\x00" + cstr(key))
                write_map(value)
            elif isinstance(value, str):
                out.extend(b"\x01" + cstr(key) + cstr(value))
            elif isinstance(value, UInt64):
                out.extend(b"\x07" + cstr(key) + struct.pack("<Q", value))
            elif isinstance(value, Float32):
                out.extend(b"\x03" + cstr(key) + struct.pack("<f", value))
            else:
                out.extend(b"\x02" + cstr(key) + struct.pack("<i", value))
        out.extend(b"\x08")

    write_map(obj)
    return bytes(out)


def read_shortcuts(vdf_path):
    """Returns (parsed vdf, games). Each game keeps a reference to its vdf entry for editing."""
    root = parse_binary_vdf(vdf_path.read_bytes())
    shortcuts = next((v for k, v in root.items() if k.lower() == "shortcuts"), {})
    games = []
    for raw in shortcuts.values():
        entry = {k.lower(): v for k, v in raw.items()}
        name = str(entry.get("appname", "")).strip()
        if not name:
            continue
        exe = str(entry.get("exe", ""))
        if isinstance(entry.get("appid"), int):
            shortcut_id = entry["appid"] & 0xFFFFFFFF
        else:  # older shortcuts.vdf files don't store the id; Steam derives it like this
            shortcut_id = (zlib.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000
        icon_key = next((k for k in raw if k.lower() == "icon"), "icon")
        games.append({"name": name, "id": shortcut_id, "entry": raw, "icon_key": icon_key})
    return root, games


def current_icon(game):
    """The shortcut's icon file if it's set and exists, else None."""
    icon = str(game["entry"].get(game["icon_key"], "")).strip().strip('"')
    return Path(icon) if icon and Path(icon).is_file() else None


def set_icon(game, path):
    game["entry"][game["icon_key"]] = str(path)
    game["icon_changed"] = True


def steam_running():
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                                 capture_output=True, text=True).stdout
            return "steam.exe" in out.lower()
        return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0
    except OSError:
        return False


def existing_art(grid, shortcut_id, suffix):
    return [grid / f"{shortcut_id}{suffix}{ext}" for ext in IMAGE_EXTS
            if (grid / f"{shortcut_id}{suffix}{ext}").exists()]


# --------------------------------------------------------------------------- name matching

def normalize(name):
    name = re.sub(r"[™®©]", "", name.lower()).replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name).split())


def clean_name(name):
    """Strip launcher/platform noise from a shortcut name for searching."""
    name = re.sub(r"[™®©]", "", name)
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name)  # (x64), [GOG], ...
    name = re.sub(r"\s*[-–:]?\s*\b(launcher|x64|x86|64-?bit|32-?bit|win64|dx11|dx12|vulkan)\s*$",
                  "", name, flags=re.IGNORECASE)
    return " ".join(name.split()) or name


def search_terms(name):
    cleaned = clean_name(name)
    return [name] if cleaned == name else [name, cleaned]


def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def best_match(name, candidates):
    """candidates: [(id, name)] in relevance order. Returns (id, name, score) or None."""
    best = None
    for cand_id, cand_name in candidates:
        score = max(similarity(term, cand_name) for term in search_terms(name))
        if best is None or score > best[2]:
            best = (cand_id, cand_name, score)
    return best if best and best[2] >= MATCH_THRESHOLD else None


# --------------------------------------------------------------------------- sources

def steam_search(name):
    candidates = []
    for term in search_terms(name):
        query = urllib.parse.urlencode({"term": term, "l": "english", "cc": "US"})
        try:
            items = get_json(f"https://store.steampowered.com/api/storesearch/?{query}").get("items", [])
        except (urllib.error.URLError, ValueError, OSError):
            continue
        candidates += [(it["id"], it["name"]) for it in items if it.get("type") == "app"]
    return best_match(name, candidates)


def steam_store_item(appid):
    """Store info (name, assets) for a Steam app, or None if it doesn't exist."""
    request = {"ids": [{"appid": appid}],
               "context": {"language": "english", "country_code": "US"},
               "data_request": {"include_assets": True}}
    try:
        data = get_json("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json="
                        + urllib.parse.quote(json.dumps(request)))
        item = data["response"]["store_items"][0]
    except (urllib.error.URLError, ValueError, KeyError, IndexError, OSError):
        return None
    return item if item.get("success") == 1 else None


def steam_app_info(appid):
    """Steam's app info "common" section (via api.steamcmd.net).

    Has the hashed library asset paths the store API doesn't list (e.g. newer logos)
    and the client icon."""
    try:
        data = get_json(f"https://api.steamcmd.net/v1/info/{appid}")
        return data["data"][str(appid)]["common"]
    except (urllib.error.URLError, ValueError, KeyError, TypeError, OSError):
        return {}


def steam_asset_urls(appid):
    """Candidate image URLs per kind for a Steam app, best first."""
    urls = {kind: [] for kind in KINDS}
    info = steam_app_info(appid)
    if info.get("clienticon"):
        urls["icon"].append(f"https://shared.steamstatic.com/community_assets/images/apps/"
                            f"{appid}/{info['clienticon']}.ico")
    library = info.get("library_assets_full", {})
    for kind, key, sizes in (("logo", "library_logo", ("image2x", "image")),
                             ("wide", "library_header", ("image2x",))):  # 920x430
        for size in sizes:
            path = library.get(key, {}).get(size, {}).get("english")
            if path:
                urls[kind].append(f"{STEAM_ASSET_BASE}steam/apps/{appid}/{path}")
    try:
        assets = steam_store_item(appid)["assets"]
        url_format = assets["asset_url_format"]
        for kind, keys in (("cover", ("library_capsule_2x", "library_capsule")),
                           ("wide", ("header_2x", "header")),
                           ("hero", ("library_hero", "library_hero_2x")),
                           ("logo", ("library_logo_2x", "library_logo"))):
            for key in keys:
                if assets.get(key):
                    urls[kind].append(STEAM_ASSET_BASE + url_format.replace("${FILENAME}", assets[key]))
    except (TypeError, KeyError):
        pass
    # Older unhashed CDN paths (also the only place the logo is served from)
    legacy = f"{STEAM_ASSET_BASE}steam/apps/{appid}/"
    urls["cover"] += [legacy + "library_600x900_2x.jpg", legacy + "library_600x900.jpg"]
    urls["wide"] += [legacy + "header.jpg"]
    urls["hero"] += [legacy + "library_hero.jpg"]
    urls["logo"] += [legacy + "logo.png"]
    return urls


class SteamGridDB:
    QUERIES = {
        "cover": ("grids", {"dimensions": "600x900,342x482,660x930", "mimes": "image/png,image/jpeg"}),
        "wide": ("grids", {"dimensions": "920x430,460x215", "mimes": "image/png,image/jpeg"}),
        "hero": ("heroes", {"mimes": "image/png,image/jpeg"}),
        "logo": ("logos", {"mimes": "image/png"}),
        "icon": ("icons", {"mimes": "image/png,image/vnd.microsoft.icon"}),
    }

    def __init__(self, api_key):
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _get(self, path, params=None):
        url = SGDB_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = get_json(url, self.headers)
        return data.get("data") if data.get("success") else None

    def check_key(self):
        try:
            self._get("/search/autocomplete/portal")
            return True
        except urllib.error.HTTPError as e:
            return e.code != 401
        except (urllib.error.URLError, OSError):
            return True  # network problem, not a bad key

    def find_game(self, name, steam_appid=None, search_name=True):
        if steam_appid:
            try:
                game = self._get(f"/games/steam/{steam_appid}")
                if game:
                    return game["id"], game["name"]
            except (urllib.error.URLError, ValueError, OSError):
                pass
        if not search_name:
            return None
        candidates = []
        for term in search_terms(name):
            try:
                results = self._get("/search/autocomplete/" + urllib.parse.quote(term, safe="")) or []
            except (urllib.error.URLError, ValueError, OSError):
                continue
            candidates += [(g["id"], g["name"]) for g in results]
        match = best_match(name, candidates)
        return (match[0], match[1]) if match else None

    def urls(self, game_id, kind):
        endpoint, params = self.QUERIES[kind]
        params = {**params, "types": "static", "nsfw": "false", "humor": "false"}
        try:
            results = self._get(f"/{endpoint}/game/{game_id}", params) or []
        except (urllib.error.URLError, ValueError, OSError):
            return []
        return [r["url"] for r in results if r.get("url")]


# --------------------------------------------------------------------------- download

def extension_for(url, content_type):
    content_type = content_type.lower()
    if "icon" in content_type:  # image/x-icon, image/vnd.microsoft.icon
        return ".ico"
    if "png" in content_type:
        return ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ".jpg" if ext == ".jpeg" else (ext if ext in IMAGE_EXTS else None)


def download_first(urls, grid, shortcut_id, suffix, dry_run):
    """Save the first URL that returns a usable image. Returns the saved path or None."""
    for url in urls:
        try:
            data, content_type = http_get(url)
        except (urllib.error.URLError, OSError):
            continue
        ext = extension_for(url, content_type)
        if not ext or len(data) < 1024:
            continue
        path = grid / f"{shortcut_id}{suffix}{ext}"
        if not dry_run:
            for old in existing_art(grid, shortcut_id, suffix):  # avoid a stale .jpg next to a new .png
                old.unlink()
            path.write_bytes(data)
        return path
    return None


# --------------------------------------------------------------------------- main flow

def fetch_artwork(game, grid, wanted, appid, sgdb, args, search_name=True):
    """Download `wanted` kinds from Steam app `appid`, then SteamGridDB. Returns {kind: source}."""
    found = {}
    if appid:
        urls = steam_asset_urls(appid)
        for kind in wanted:
            if download_first(urls[kind], grid, game["id"], KINDS[kind], args.dry_run):
                found[kind] = "Steam"

    remaining = [k for k in wanted if k not in found]
    if remaining and sgdb:
        sg_game = sgdb.find_game(game["name"], appid, search_name)
        if sg_game:
            print(f"  SteamGridDB match: {sg_game[1]} (id {sg_game[0]})")
            for kind in remaining:
                if download_first(sgdb.urls(sg_game[0], kind), grid, game["id"], KINDS[kind], args.dry_run):
                    found[kind] = "SteamGridDB"
        else:
            print("  SteamGridDB: no match")
    return found


def parse_appid(text):
    """Accept a plain AppID or a Steam store URL like store.steampowered.com/app/1145360/Hades."""
    match = re.search(r"/app/(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    return int(match.group(1)) if match else None


def ask_steam_appid(game_name, missing):
    print(f"  Missing {', '.join(missing)} for '{game_name}'.")
    while True:
        try:
            text = input("  Enter the Steam AppID or store URL (Enter to skip): ").strip()
        except EOFError:
            return None
        if not text:
            return None
        appid = parse_appid(text)
        item = steam_store_item(appid) if appid else None
        if item:
            print(f"  AppID {appid} = {item.get('name', '?')}")
            return appid
        print("  Not a valid Steam AppID, try again.")


def has_art(game, grid, kind):
    if kind == "icon":  # the shortcut must point at an existing icon file
        return current_icon(game) is not None
    return bool(existing_art(grid, game["id"], KINDS[kind]))


def process_game(game, grid, sgdb, config, args):
    print(f"\n[{game['name']}]  (id {game['id']})")
    if not args.refresh and not current_icon(game):
        downloaded = existing_art(grid, game["id"], KINDS["icon"])
        if downloaded:  # downloaded on an earlier run while Steam was open
            set_icon(game, downloaded[0])
    wanted = [k for k in KINDS if args.refresh or not has_art(game, grid, k)]
    if not wanted:
        print("  all artwork already present, skipping")
        return {}

    saved_ids = config.setdefault("steam_ids", {})
    appid = saved_ids.get(game["name"])
    if appid:
        print(f"  Steam AppID from {CONFIG_FILE.name}: {appid}")
    else:
        steam = steam_search(game["name"])
        if steam:
            appid = steam[0]
            print(f"  Steam match: {steam[1]} (appid {appid}, {steam[2]:.0%})")
        else:
            print("  Steam: no confident match")
    found = fetch_artwork(game, grid, wanted, appid, sgdb, args)

    missing = [k for k in wanted if k not in found]
    if missing and args.can_prompt and game["name"] not in saved_ids:
        new_appid = ask_steam_appid(game["name"], missing)
        if new_appid:
            if not args.dry_run:
                saved_ids[game["name"]] = new_appid
                save_config(config)
                print(f"  Saved AppID to {CONFIG_FILE.name}")
            if new_appid == appid:  # same game, only fetch what's still missing
                found.update(fetch_artwork(game, grid, missing, new_appid, sgdb, args, search_name=False))
            else:  # different game: replace everything from the new ID
                found = fetch_artwork(game, grid, wanted, new_appid, sgdb, args, search_name=False)

    if "icon" in found and not args.dry_run:
        set_icon(game, existing_art(grid, game["id"], KINDS["icon"])[0])

    for kind in KINDS:
        if kind in found:
            status = f"OK  from {found[kind]}" + (" (dry run, not saved)" if args.dry_run else "")
        elif kind in wanted:
            status = "--  not found"
        else:
            status = "    already present"
        print(f"  {kind:<6} {status}")
    return {k: found.get(k) for k in wanted}


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"Could not save {CONFIG_FILE.name}: {e}")


def get_sgdb_key(config, args):
    if args.no_sgdb:
        return None
    key = (args.sgdb_key or os.environ.get("SGDB_API_KEY") or config.get("steamgriddb_api_key") or "").strip()
    from_user = bool(args.sgdb_key)
    if not key and args.can_prompt:
        print(f"SteamGridDB API key not set in {CONFIG_FILE.name} (free key: {SGDB_KEY_URL})")
        try:
            key = input("Paste your key, or press Enter to use Steam only: ").strip()
        except EOFError:
            key = ""
        from_user = bool(key)
    if key and not SteamGridDB(key).check_key():
        print("SteamGridDB key was rejected (401). Continuing with Steam only.")
        return None
    if key and from_user:
        config["steamgriddb_api_key"] = key
        save_config(config)
        print(f"Saved key to {CONFIG_FILE.name}")
    return key or None


def main():
    parser = argparse.ArgumentParser(description="Download Steam library artwork for non-Steam games.")
    parser.add_argument("--steam-path", help="Steam install folder (auto-detected if omitted)")
    parser.add_argument("--user", help="only this Steam userdata ID")
    parser.add_argument("--only", help="only games whose name contains this text")
    parser.add_argument("--refresh", "--overwrite", dest="refresh", action="store_true",
                        help="re-download all artwork, replacing what already exists")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, save nothing")
    parser.add_argument("--sgdb-key", help="SteamGridDB API key (saved for next time)")
    parser.add_argument("--no-sgdb", action="store_true", help="use Steam only")
    parser.add_argument("--no-prompt", action="store_true",
                        help="don't ask for a Steam AppID when artwork isn't found")
    args = parser.parse_args()
    args.can_prompt = sys.stdin.isatty() and not args.no_prompt

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # game names with symbols on old Windows consoles

    steam_path = find_steam_path(args.steam_path)
    if not steam_path or not (steam_path / "userdata").is_dir():
        sys.exit("Steam folder not found. Pass it with --steam-path.")
    print(f"Steam: {steam_path}")

    vdf_files = sorted((steam_path / "userdata").glob("*/config/shortcuts.vdf"))
    if args.user:
        vdf_files = [f for f in vdf_files if f.parents[1].name == args.user]
    if not vdf_files:
        sys.exit("No non-Steam shortcuts found (no shortcuts.vdf in userdata).")

    config = load_config()
    key = get_sgdb_key(config, args)
    sgdb = SteamGridDB(key) if key else None

    steam_open = steam_running()
    if steam_open:
        print("Steam is running: icons will be downloaded, but they can only be set once Steam is closed.")

    missing_report = []
    total_saved = 0
    icons_pending = 0
    for vdf in vdf_files:
        user_id = vdf.parents[1].name
        try:
            raw = vdf.read_bytes()
            root, games = read_shortcuts(vdf)
        except (OSError, ValueError) as e:
            print(f"\nCould not read {vdf}: {e}")
            continue
        all_games = games
        if args.only:
            games = [g for g in games if args.only.lower() in g["name"].lower()]
        print(f"\n=== Steam user {user_id}: {len(games)} non-Steam game(s) ===")
        grid = vdf.parent / "grid"
        if not args.dry_run:
            grid.mkdir(parents=True, exist_ok=True)
        for game in games:
            try:
                result = process_game(game, grid, sgdb, config, args)
            except Exception as e:  # one bad game shouldn't stop the run
                print(f"  error: {e}")
                continue
            total_saved += sum(1 for src in result.values() if src)
            missing = [k for k, src in result.items() if not src]
            if missing:
                missing_report.append(f"{game['name']}: {', '.join(missing)}")
            time.sleep(0.3)  # be polite to the APIs

        changed = sum(1 for g in all_games if g.get("icon_changed"))
        if changed and not args.dry_run:
            if steam_open:  # Steam would overwrite shortcuts.vdf on exit
                icons_pending += changed
            elif dump_binary_vdf(parse_binary_vdf(raw)) != raw:
                print(f"\nNot setting icons: {vdf} has a format this script can't safely rewrite.")
            else:
                backup = vdf.with_name("shortcuts.vdf.bak")
                shutil.copy2(vdf, backup)
                vdf.write_bytes(dump_binary_vdf(root))
                print(f"\nSet {changed} icon(s) in {vdf} (backup: {backup.name})")

    print("\n" + "=" * 50)
    print(f"{'Would download' if args.dry_run else 'Downloaded'} {total_saved} image(s).")
    if missing_report:
        print("Still missing:")
        for line in missing_report:
            print(f"  - {line}")
    if icons_pending:
        print(f"{icons_pending} icon(s) downloaded but not set because Steam is open. "
              "Exit Steam completely, then run this script again to apply them.")
    if not args.dry_run and total_saved:
        print("Restart Steam (or go back to Game Mode on Steam Deck) to see the new artwork.")


if __name__ == "__main__":
    main()
