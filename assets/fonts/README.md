# Fonts

**Albert Sans** (`AlbertSans[wght].ttf`, a variable font covering weights 100-900), (c) The Albert Sans Project Authors,
https://github.com/usted/Albert-Sans, licensed under the **SIL Open Font License 1.1** (full text: `OFL-AlbertSans.txt`).
Source of this copy: the Google Fonts repository (`ofl/albertsans`). The font is bundled unmodified.

`clickflow/gui/theme.py` registers every `.ttf` / `.otf` in this folder at start-up (`load_fonts()`).

* The interface uses Regular (400) and, for light emphasis, Medium (500); nothing is bold.
* Albert Sans has no CJK glyphs, so Qt falls back per glyph to Segoe UI / Microsoft YaHei UI for Chinese text and for any
  symbol the font lacks.

The whole `assets/` folder is bundled into `ClickFlow.exe` by `clickflow.spec`.
