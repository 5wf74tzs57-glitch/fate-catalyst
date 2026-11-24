# Fate / Catalyst

A low-poly 3D trickster sandbox. You don't cause disasters, you start them: plant small catalysts at a busy city intersection, hit **Release**, and get 45 seconds of consequences, a slow-motion replay, and an itemized insurance claim.

## Play

Open `index.html` in a modern desktop browser (Chrome, Edge, Firefox, Safari). It needs an internet connection the first time, because it loads three.js and cannon-es from the jsDelivr CDN.

To host it, turn on GitHub Pages for this repository (Settings → Pages → Deploy from a branch → `main`, folder `/`). The game is then playable at `https://<user>.github.io/<repo>/`.

Some browsers block ES modules on `file://` pages. If double-clicking `index.html` shows a blank screen, serve the folder locally instead:

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000.

## How a round works

1. **Recon.** Time is frozen. Hover people to read their day. Two of them are at their breaking point, and anything they cause during a meltdown counts double. Press **P** or **▶ Run time** to watch traffic first.
2. **Plant catalysts.** You have 100 Fate to spend on a plastic bag, thumbtack, skateboard, soccer ball, golf ball, car door, magic scissors, rabid squirrel, drunk guy or lightning, plus rain, fog and wind.
3. **Release.** You get 45 seconds of consequences, and leftover Fate can be spent mid-run.
4. **Replay and claim.** The replay is titled after your most destructive catalyst. The claim itemizes injuries, vehicles, property and emotional distress, and every line shows the chain of cause and effect that led to it.

## Controls

| Input | Action |
| --- | --- |
| Drag | Orbit the camera |
| Right-drag / two fingers | Pan |
| Scroll / pinch | Zoom |
| Click a catalyst, then click the map | Place it |
| Drag on the map (skateboard, soccer ball) | Aim and set power |
| Esc / right-click | Cancel the selected catalyst |
| P | Pause or run time during recon |
| Space | Release |

## Tech

One self-contained HTML file: [three.js](https://threejs.org) 0.160 for rendering and [cannon-es](https://github.com/pmndrs/cannon-es) 0.20 for physics and ragdolls, with sound synthesized by the Web Audio API. There's no build step.
