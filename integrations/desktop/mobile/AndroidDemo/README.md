# Cognee Mobile Demo (Android)

One screen: opens straight onto the answer your desktop knowledge graph
gives for **"what lenovo competitors there are"**. Live when it can reach
the backend, bundled copy when it can't — the demo never opens empty.

No dependencies: a single Java activity, platform widgets only.

## Build

Open this folder in **Android Studio** (Giraffe or newer) and press Run —
or from a machine with the Android SDK + JDK 17:

```bash
cd integrations/desktop/mobile/AndroidDemo
gradle wrapper --gradle-version 8.7   # first time only
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Point it at the backend

Easiest (USB, zero config): the app defaults to `http://127.0.0.1:8765`,
so forward the phone's localhost to the Mac over USB:

```bash
adb reverse tcp:8765 tcp:8765
```

Then open the app — the status line flips from *"bundled copy"* to
*"live · from team handover + your files"* as the real answer arrives.

On Wi-Fi instead: set `BACKEND` in `MainActivity.java` to your Mac's LAN
IP (e.g. `http://192.168.1.20:8765`) and make sure the backend listens on
that interface (`COGNEE_DESKTOP_HOST=0.0.0.0` in backend.env, then restart).

## Refresh the bundled answer

The offline copy lives at `app/src/main/assets/answer.txt`. Regenerate it
any time:

```bash
curl -s "localhost:8765/search?q=what%20lenovo%20competitors%20there%20are&mode=answer" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['answer'].replace('**',''))" \
  > app/src/main/assets/answer.txt
```
