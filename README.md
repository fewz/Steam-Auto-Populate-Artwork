# Steam artwork for non-Steam games

Fills in the **cover**, **wide cover**, **background (hero)**, **logo** and **icon** for every
non-Steam game in your Steam library. Looks on the Steam store first, then SteamGridDB.

Needs Python 3.8+ (already on Steam Deck). No extra packages.

## Windows
1. Install Python from python.org (tick "Add Python to PATH").
2. Double-click `run_windows.bat`, or run `python steam_artwork.py`.

## Steam Deck
1. Switch to Desktop Mode, open **Konsole**.
2. `cd` to the folder with the script and run `python3 steam_artwork.py`.
3. Go back to Game Mode to see the artwork.

## SteamGridDB key
Get a free API key at https://www.steamgriddb.com/profile/preferences/api and paste it
into `config.json` next to the script:

```json
{
  "steamgriddb_api_key": "your-key-here"
}
```

If the key is empty, the script asks for it on first run and saves it to `config.json`.
Press Enter at that prompt to skip and use Steam only.

## Icons (close Steam first)
Steam reads a shortcut's icon from `shortcuts.vdf`, not the grid folder, so the script
writes the icon's path into that file. Steam overwrites it when it exits, so this only
works while **Steam is fully closed**:

- **Windows:** right-click the Steam tray icon → Exit, then run the script.
- **Steam Deck:** in Desktop Mode, Steam menu → Exit Steam, then run the script in Konsole.

If Steam is open, the icons are still downloaded, and the script tells you to close Steam
and run it again; the second run sets them without downloading again.
A backup of the previous file is saved as `shortcuts.vdf.bak`.

## When artwork isn't found
If a game is still missing artwork after searching, the script asks for its Steam AppID.
You can type the number (e.g. `1145360`) or paste the store URL
(`https://store.steampowered.com/app/1145360/Hades/`). Press Enter to skip.

The ID is saved in `config.json` under `steam_ids`, so later runs (including `--refresh`)
use it directly instead of searching:

```json
{
  "steamgriddb_api_key": "your-key-here",
  "steam_ids": {
    "My Game Shortcut Name": 1145360
  }
}
```

To fix a wrong ID, edit or delete its line in `config.json`.

## Options
| Option | What it does |
|---|---|
| `--dry-run` | Show what would be downloaded, save nothing |
| `--refresh` | Re-download all artwork, replacing what you already have |
| `--only "name"` | Only games whose name contains this text |
| `--user 12345678` | Only this Steam account (userdata folder ID) |
| `--steam-path PATH` | Steam folder, if it isn't found automatically |
| `--sgdb-key KEY` | Set the SteamGridDB key without the prompt |
| `--no-sgdb` | Steam store only |
| `--no-prompt` | Never ask for a Steam AppID (for unattended runs) |

Games that already have artwork are skipped, so it's safe to run again after adding new shortcuts.
Images go in `userdata/<id>/config/grid/`. The only change to your shortcuts is the icon path
(see above), and only while Steam is closed.
