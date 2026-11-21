# Ideas

Work queue for the music box. When the current task is done, continue
with the next open item here. Mark items `[x]` when built and checked.

## Open

- [x] **Play Mode (fullscreen Now Playing).** A "fullscreen" option on the
  Now Playing tile that fills the screen with a "Play Mode": large artwork
  (if there is any), the track details, and the playback controls.

- [x] **Next / Previous.** Skip to the next or previous track when the card
  plays more than one track.

- [x] **Multi-track cards.** When editing a card, select several tracks and
  arrange them in a chosen order. The track selection list no longer shows
  folders.

- [x] **Shuffle (during playback).** Available when the card has more than 2
  tracks. Always starts as "No shuffle". Shuffling rearranges the order of
  the current playback only (the card's saved order is unchanged), and keeps
  track of which tracks have already played, so it knows when to stop, or
  when to start over if Repeat is on.

- [x] **Repeat.** Set a card's playback to repeat. With several tracks, play
  them all in order (the card's order, or the shuffled order if Shuffle is
  on), then start again from the beginning.

- [x] **File manager on the website.** Manage the files in the music folder
  from the page and upload new ones, instead of copying files onto the Pi by
  hand. Show the folder's location on the Pi somewhere, for going into the
  filesystem directly when troubleshooting.

- [x] **Tap log changes.** Date in the first column; drop the # column. Show
  only the latest 10 taps, with a "View more" button below that opens a modal
  with a table of all taps: 100 per page with pagination, and a search.

- [x] **Edit window: checkboxes, not switches.** Shuffle/Repeat (and the new
  Remember playback) are saved with the card, so they're checkboxes.

- [x] **Card artwork saves with the card.** Uploading, picking from
  MusicBrainz or removing artwork only takes effect when you press Save;
  Cancel discards it.

- [x] **Remember playback (per card).** A `rememberPlayback` flag, off by
  default, set by a checkbox in the Edit and Assign windows. When on, the
  next tap resumes where the card left off.

- [x] **Wi-Fi setup.** On start-up with no network, try the saved Wi-Fi
  networks; if none connects, host an open "Tap to Play Setup" network. A
  phone that joins gets a page demanding Wi-Fi details; they're saved and
  the box restarts. A password rejected twice in a row deletes that saved
  network. Errors go to a timestamped log, readable from the setup page.
  Saved networks are editable in a new Settings window (gear button, top
  right), which also takes over the light/dark switch.

## Decisions (2026-11-20)

- **Shuffle turned on mid-playback:** the current track keeps playing; then
  *all* the other tracks play in a fresh random order, including ones that
  already played. Turning Shuffle off continues with the tracks after the
  current one, in the card's order.
- **Repeat + Shuffle:** every round gets a new random order (and never starts
  with the track that just ended the previous round).
- **Saved on the card:** Shuffle and Repeat are both card settings, stored in
  `cards.json` with the card (new cards: both off). Toggling them during
  playback changes the card's setting and the current playback.
- **Folder cards become track lists:** existing folder cards are converted to
  the list of their files, in the same order. Files added to the folder later
  are not added to the card automatically.
- **Shuffle is offered at 2 or more tracks.**
- **Order of work:** multi-track cards first (the others build on it), then
  Next/Previous, then Shuffle and Repeat, then Play Mode.

## Notes

- Sound rules still apply to everything here: sound only follows a card
  tap, the volume is checked before and during playback, and the web page
  never starts a song by itself. Next/Previous, Shuffle and Repeat act only
  on the song from the current tap while its card is on the reader, like
  pause and seek do.

## Decisions (2026-11-21)

- **Remember playback:** resumes the same track 5 seconds before where it
  stopped; after every track has played to the end it starts from the top
  again. Saved when the card is lifted and every ~15 s while playing.
- **Setup network:** open (no password), named "Tap to Play Setup".
- **Ethernet counts as a network:** with a cable connected, setup mode
  never starts.
- **Boot wait:** 3 minutes for a saved network before setup mode starts.
- **No retries in setup mode:** it waits until someone enters Wi-Fi details
  (or the box is restarted).
- **Wrong password:** deleted after 2 rejections in a row; a network that is
  simply out of range is never deleted.
- **Assign window** gets the same Shuffle / Repeat / Remember playback
  checkboxes as Edit.
