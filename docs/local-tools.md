# Optional web search and weather

Domuno can offer two read-only tools to Qwen on a new user turn, after the command router. The gateway handles local calls within the original Home Assistant request. If Qwen chooses an HA tool instead, HA still executes it. A response that mixes local calls with HA actions is rejected without executing either.

## Enable

Set the desired values in the gateway environment (see [`.env.example`](../.env.example)), then restart the gateway using the usual deployment procedure. No new HA integration or tool configuration is required.

- **Search:** `BRAVE_SEARCH_API_KEY` enables Brave Web Search. The model-generated query goes to Brave; the gateway does not attach history or an entity list. The query can still contain spoken details; leave the tool off if this is unacceptable. Use gateway authentication and a trusted LAN to protect the quota.
- **Weather:** set `WEATHER_LATITUDE`, `WEATHER_LONGITUDE` and optionally `WEATHER_LOCATION` (spoken display label). Open-Meteo receives the selected coordinates. If the question names a city, its name goes to Open-Meteo geocoding and the top match is used. Supply a qualified city name when needed. Forecast dates use the selected location's timezone.
- **Optional speech cue:** `SEARCH_CUE_FILE` points at a local WAV, e.g. a prerecorded “Sprawdzam w internecie”. It plays through the host's `paplay` and uses `LLM_WAIT_SOUND_VOLUME` (follows LVA volume and mute by default). Leave unset for no search cue. No recording is bundled.

Clear the key or both coordinates to disable the corresponding tool, and restart the gateway.

## Smoke test on Arduino VENTUNO Q

1. Deploy the `meteo_brave` code to the existing Domuno gateway checkout using your normal update process. To run the offline suite on the board first, use `BOARD_HOST=arduino@YOUR_BOARD DEPLOY_DIR=/home/arduino/domuno-meteo-test bash tools/sync_board.sh test` from the branch checkout. This copies files known to Git to a separate directory, not the active service.
2. On the board, run `systemctl --user show qwen-ha-gateway.service -p WorkingDirectory` and edit `.env` in that directory. Keep your existing settings. Get a key from the [Brave Search API dashboard](https://api-dashboard.search.brave.com/) and set `BRAVE_SEARCH_API_KEY` privately on the board; do not commit it or paste it into chat. No key is needed for Open-Meteo. For Warsaw city centre set:

   ```dotenv
   WEATHER_LATITUDE=52.2297
   WEATHER_LONGITUDE=21.0122
   WEATHER_LOCATION="Warszawa"
   ```

3. Restrict `.env` to your user (`chmod 600 .env`), then run `systemctl --user restart qwen-ha-gateway.service` and `systemctl --user is-active qwen-ha-gateway.service`.
4. In the existing Assist pipeline try “Czy jutro będzie padało?” (Warsaw), “Jaka będzie pogoda jutro w Gdańsku?” (geocoding), and “Sprawdź w internecie, jakie są dziś najnowsze wiadomości z Warszawy” (Brave). Also try a regular non-current question and a harmless existing device command as controls. Where gateway metrics are enabled, look for `route=local_tool` and `local_tool_ms` on the information requests. A route hit alone does not prove factual accuracy; compare the answer with the source. If a restart fails, inspect `journalctl --user -u qwen-ha-gateway.service -n 30 --no-pager`.

## Context and latency

Search selects up to three short extracts from different domains, with lexical overlap and provider rank as signals. It does not fetch result pages. Weather contributes the current conditions and up to three daily summaries. The evidence is bounded by result count and character limits, not an exact token count; the gateway runs its full prompt counter for both model rounds. No model call is spent summarizing search results before answering.

The first model completion is buffered so an internal call cannot escape to HA. A selected local tool runs once. For the second model round the gateway sends only the latest question and compact evidence in a fresh prompt without tools or the HA inventory; only final text can go back to HA. Provider errors or empty results produce a short fallback instead of asking the model to guess. The fresh prompt does not assume a KV-cache hit. Validate live GenieX tool selection and voice latency before enabling broadly.
