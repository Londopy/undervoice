# Changelog

All notable changes to undervoice are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is what appears on its GitHub release page.

## [Unreleased]

### Added

- Text messages to another station, or to everyone: up to 384 characters, sent 24 to a frame in place of the station frame and put back together on receive, with gaps shown where a part was missed. A part counts as sent only once speech has carried its frame, so keying without talking sends nothing. **Messages** in the window has a counter of how much talking a message needs, **Send again** to fill a receiver's gaps, **Cancel**, and the whole text of the message you pick; **Send a recording...** carries queued parts too. On the command line, `tx --to CALL --msg TEXT`, and `rx` and `decode` print the messages they hear. Stations on 0.5.0 or older ignore message frames.
- **Save messages to a text file** in the window, or `--messages FILE` on `tx` and `rx`, adds every message sent and heard to a plain text file, one line each, in UTC. `tx` and `rx` check the file can be written before they start.
- Closing the window with message parts still to go asks first.
- `tx` without `--cw-id` reminds you to give your callsign by voice.
- `Part` and `split` in the Python API. A decoded `Frame.message` is now a `Message` or a `Part`.

### Changed

- The default data level under the voice is -21 dB, down from -18 dB, after the first blind listening test heard the data at -18 dB. It still decodes every frame on a clean simplex or repeater path, but loses more on a noisy one; `--level -18` or **Data level** trades that back. The window moves to -21 dB too, unless you had set a level of your own.
- `abx` tests -24, -21 and -18 dB by default.
- The window puts Messages, Received and Updates on tabs and fits a 1366x768 screen.
- The README states the sub-audible mode's rate, about 43 bit/s of payload, and softens claims that nothing measured: how far a receiver's filter takes the sub-audible signal down, the virtual cable check, and the Part 97 reading, which is now marked as an interpretation.

## [0.5.0] - 2026-11-19

### Added

- A sub-audible mode (`--mode sub`, or **Mode** in the window): the same payload as DBPSK on a 150 Hz carrier below the voice band, a frame every 5.9 s at a constant level, for radios with a flat 9600-baud data port on tone-free simplex channels. `channel --path flat` simulates that port. Changing **Mode** while the window listens starts listening again in the new mode.
- Setup checks: **Test microphone** in the window (or `undervoice check`) records you, rates the mic level, mixes in your data and decodes it, and plays back what would be sent on the default output; **Test PTT** (or `check --ptt`) keys the radio for 2 seconds.
- `undervoice abx`: a blind ABX listening test, with results saved to CSV, combined across listeners and scored per level, or the clips exported for playing another way. `--pause-level` tests data kept up through pauses.
- `--pause-level` and **In pauses** in the window keep the data going through pauses at a set level below the recent speech, so frames survive pauses on noisy paths. Off by default, and never above the data level.
- `--cw-id` and **CW ID before unkeying, when due** in the window end the first over, and every over that ends 9 minutes or more after the last ID, with your callsign in Morse, a minute early since it can only go out as an over ends; `tx --play --cw-id` ends the recording with it. The window reminds you to identify when an over runs past 9 minutes, and without CW ID every 9 minutes while the station is in use.
- `sweep` prints a 95% upper bound on the frame loss rate, and a slow test (`pytest -m slow`, run weekly in CI) shows the under-1% target over 480 frames.
- README sections on design choices, prior art and the listening test.

### Fixed

- A WAV file that is missing or is not a WAV is reported by name instead of with a traceback.
- PTT lets go of the serial port, USB interface or rigctld connection even when unkeying fails, so the next try can open it again.
- A USB interface that refuses the PTT command is reported as an error instead of being ignored.

## [0.4.0] - 2026-11-19

### Added

- The window starts listening as soon as it opens. Untick **Listen whenever undervoice is open** to start it by hand instead; the choice is remembered.

## [0.3.0] - 2026-11-19

### Added

- Install with Scoop: `scoop bucket add londopy https://github.com/Londopy/scoop-bucket`, then `scoop install londopy/undervoice`.
- Hold Space in the window to talk, and let go to stop. Buttons there take Enter instead of Space.
- A fresh frame starts whenever speech comes back after 3 s of silence, so overs keyed by the radio's own PTT or a VOX interface line up with frames.

## [0.2.0] - 2026-11-19

### Added

- `tx --ptt` keys the radio through a CM108-type USB interface's GPIO (DMK URI, AIOC), a serial port's RTS or DTR line (Digirig), or a CAT command through Hamlib's rigctld, with `--txdelay` for the radio to come up to power. In mic mode Enter keys and unkeys, and each transmission starts a fresh frame.
- `undervoice devices` lists every way this machine can key the radio.
- A window (`undervoice gui`, or `undervoice-gui` with no console on Windows) with station details, sound card and PTT pickers, transmit and send-a-recording buttons, an input level meter and a table of frames heard. It follows the Windows light or dark setting and accent color.
- The window remembers your station, sound cards, PTT and levels in `config.toml`.
- Update checks against GitHub Releases, on a schedule and on demand. An update installs only when you click **Download and install**, and only after it matches the release's SHA-256.
- Windows downloads: a per-user installer, an MSI, a portable exe and zip, `install.ps1` and `install.cmd`, with torrents and magnet links for each and `SHA256SUMS.txt` over all of them.
- `undervoice --version`.

### Changed

- Needs Python 3.11 or newer; 3.10 reached its end of life in October 2026.

## [0.1.0] - 2026-11-19

### Added

- Frame format: callsign, grid square and 32-character status in 256 bits with CRC-16, rate 1/2 K=7 convolutional code and interleaver, one frame every 9.3 s.
- Spread-spectrum modulator that shapes the data to the speech and follows its level.
- Receiver with pilot-based acquisition, clock drift correction and sub-band combining.
- Simulated FM channel (emphasis, audio filters, deviation limiter, discriminator noise, repeater hops, sound card clock offset) and a frame-loss sweep.
- Live sound card transmit (mic or WAV file) and receive.
- `rx --record` saves received audio for offline decoding, and `rx` warns when the input clips.
- README sections on testing over the air and on the Part 97 rules.

[Unreleased]: https://github.com/Londopy/undervoice/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Londopy/undervoice/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Londopy/undervoice/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Londopy/undervoice/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Londopy/undervoice/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Londopy/undervoice/releases/tag/v0.1.0
