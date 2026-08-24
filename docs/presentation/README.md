# How DBSearch.AI Was Built - presentation kit

Everything for the talk lives in this folder.

| File | What it is |
|---|---|
| `slides/slide-01.png` … `slide-19.png` | **The slide images.** 3200x1800 (16:9 at 2x). Drop straight into PowerPoint. |
| `deck.html` | The live deck. Self-contained, no network, keyboard-driven. Present from this if you'd rather not use PowerPoint. |
| `SCRIPT.md` | The full speaker script, slide by slide, plus a Q&A appendix. |
| `LAWS.md` | The nine laws in claim / enforcement / file-path form, incl. the two honest caveats. |
| `export.sh` | Regenerates `slides/*.png` from `deck.html` after any edit. |

## Putting the images in PowerPoint

Set the deck to **16:9** (Design → Slide Size → Widescreen), then for each slide
insert the PNG and size it to fill the slide edge to edge. The images are 2x, so
they stay sharp on a 4K projector.

The speaker script in `SCRIPT.md` is written to be pasted into PowerPoint's
presenter-notes pane: each slide's **SAY** block is the notes for that slide.

## Presenting from the HTML instead

Open `deck.html` in any browser.

| Key | Does |
|---|---|
| `→` `←` `space` | Next / previous slide |
| `Home` `End` | First / last slide |
| `1`–`9` | Jump to a slide |
| `S` | Speaker notes drawer (the script, for the current slide) |
| `F` | Fullscreen |
| `P` | Print, or export to PDF |
| `?` | Key list |

Clicking the right or left half of the screen also advances or goes back, which
is what most presentation remotes send.

## Regenerating the images

After editing `deck.html`:

```bash
./export.sh
```

It serves the folder, drives headless Chrome once per slide, and writes
`slides/slide-NN.png`. Takes about 30 seconds.

## Notes on the design

- **Three accent colours, each carrying meaning, never decoration.** Blue is
  structure and protocol. Green is the guarantee, the thing that holds. Amber is
  the failure story. Rose is a refusal or a denial.
- **The gate rule** (a horizontal line with a labelled break in it) is the
  recurring device. It appears wherever something is being let through or held
  back, which is the subject of the whole talk.
- Colours are carried over from `docs/architecture_deck.html` so the two decks
  read as the same product.
