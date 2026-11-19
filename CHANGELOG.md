# Changelog

## 1.3.0 - 2026-11-18

**Interface redesign, round 2, and a much faster start** (profiles are unchanged).

* **Light theme only** - the dark theme and its toggle are gone. **English is the default language** (it no longer follows the system
  locale); switch to Chinese in the top bar. The UI font is **Albert Sans** (SIL Open Font License, bundled in `assets/fonts` with its license); nothing is bold.
* **Quieter layout:** the top bar is the profile name (menu, coral dot when unsaved), the **display preset** button, Simple / Advanced,
  the language switch and a **...** menu; the step list is a plain list; the target image is a large preview with *Re-select*, *Test this step* and a one-line
  search-area status; speed, repeat and click limit are three compact rows; one 72 px bottom bar with a dark live-log panel above it.
* **Add step** creates the step and goes straight into the screen selection; Esc removes the empty step.
* **Advanced** is three step tabs (*Step*, *Match & area*, *Waits & fallbacks*) and a *Global* tab instead of one 1400 px scroll.
* **Run dashboard:** a 40 px status word, progress, four figures, one match line. Status is a glyph shape plus a word, never colour alone.
* **Screen overlays:** selection with ink-cased strokes and square handles (cream = target, teal = search area, green = found,
  amber = candidate); the **STOP badge** now shows live status and the run result for 4 seconds.
* Dialogs show an icon only for warnings and errors.
* **The live log no longer opens by itself** when a run starts; click *Log* to see it.
* **Switches slide** (like a native toggle) and every quick click counts, also on the step list.
* **Faster start:** the main download is now a folder (`ClickFlow\ClickFlow.exe`) instead of a file that had to unpack about 80 MB on every
  start; on the test machine the window now appears in roughly 1-2 s instead of 3-4 s. The single-file `ClickFlow.exe` is still published
  (`build_exe.py --onefile`), it just opens more slowly; shorter font stacks and one stylesheet pass at start-up; switching the
  language takes about a fifth of a second, no longer flashes small windows and no longer marks the profile as changed.

## 1.2.0 - 2026-11-10

**Transparent icons and "not found" fallbacks** (profile schema 4; older profiles open unchanged, every new option is off).

* **Outline / transparent icon** matching mode: only the icon's stroke is compared, so see-through icons such as Instagram's outline
  heart are no longer missed on busy story backgrounds. *Test this step* diagnoses this case and offers the switch as a one-click fix.
* **Look again where it was last found**: a relaxed search right around the last match before giving up.
* **If still not found, click where it was last found**, guarded by a **Skip if this image is there** image (e.g. the red, liked heart),
  at most 3 blind clicks in a row and only within 5 loops of a real match.
* The Instagram example turns on outline mode and the rescue for the heart step.

**Redesigned interface** (profiles from 1.1.0 open as before).

* New look with **light and dark themes** (follows the system, switchable in the top bar) and an **English / Chinese** interface.
* **Simple and Advanced modes.** Simple shows only: the step cards, the target image, click type, *click only if found*, a three-step
  speed control, repeat count / infinite and a click limit. Advanced unfolds every setting in collapsible groups
  (*Basic, Match, Search area, Click, Waits & failure handling*) plus a *Global settings* tab (stop hotkey, timing, end condition,
  display presets, notes).
* **Run dashboard** while running: status, clicks, loops, elapsed time, current step and the latest match confidence; the big Start
  button turns into *Stop - F8*. A welcome screen guides an empty profile.
* Friendlier wording: *template -> target image*, *search box -> search area*, *threshold -> match strictness*, *Monitor 0..16 -> a
  display drop-down*. The stop hotkey is now captured by pressing the key instead of typing its name.
* Restyled screen overlays (selection, match boxes, red STOP badge). Target thumbnails are still hidden while a run is active.
* Icons: original MIT artwork (`assets/icons/icons.svg`), rendered with QtSvg.

## 1.1.0

First public release.

* Single-file Windows build (`ClickFlow.exe`) with a built-in `--selftest`; MIT licence, third-party notices, licence viewer in the Help menu.
* **Speed slider**: one control for the time between clicks (fastest = no added delay, slowest = 1.5 s) with an *Advanced* manual mode.
* **Search boxes** with an automatic "template, then search box" drag, per-step boxes shown on screen, and **display presets** that are
  selected (together with the monitor) from the detected screen resolutions.
* **Test this step** now says whether a run would click and diagnoses why not (icon size outside the scale range, outside the search box,
  threshold too strict, absent) with one-click fixes. Template scale search widened to 0.35-2.5x; the matching scale is remembered.
* Drop-downs and number boxes ignore the mouse wheel until focused (scrolling no longer changes settings); non-left click types are flagged.
* Reliable stop: hook-free stop-key polling on Windows, red STOP button, screen-corner stop.
* Saving commits half-typed values; the last used profile is reopened.

## 1.0.0

Initial version: step engine, matcher, GUI, profiles, dry run, end condition, example profile.
