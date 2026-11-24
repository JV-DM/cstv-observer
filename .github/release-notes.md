## How to use

1. Download **CSTV-Observer.exe** below and double-click it.
   - If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**. The app isn't code-signed, so Windows doesn't recognise it yet.
2. A page opens in your browser with a short checklist. Click **Start CS2** (if CS2 is open, close it first).
   Steam may ask you to confirm the launch options. Click **OK**.
3. In CS2, open a match to watch (CSTV/GOTV broadcast, spectator slot, or a demo).
4. The camera now follows the action by itself. Use **Pause** to take the camera yourself, and **Quit** to close the app.

## Required: CS2 launch options

CS2 must be started with **both** of these launch options, or the app can't move the camera:

```
-netconport 2121 -insecure
```

The **Start CS2** button tries to add them. If the checklist's camera-control step stays red, close CS2, then in Steam right-click **Counter-Strike 2** → **Properties** → **General**, paste the line above into **Launch Options**, and start CS2 again.

`-insecure` turns off VAC for that session. Use it to watch matches only, not to play, and remove both options when you want to play normally.
