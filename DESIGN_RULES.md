# Screen Audit window: design rules

The window answers one question: is the audit running, and how far along is it?

- **Type:** the system font (SF Pro on a Mac). 22 for the status headline, 15 for the day count,
  13 for body, 12 for section labels and hints. Nothing else.
- **Colour:** grayscale chrome. The ink colour (near-black, or near-white in
  dark mode) is the single accent, used for the primary button and the
  progress bar. Red means recording, amber means paused, grey means stopped.
  Colour is used for nothing else.
- **Weight of actions:** one solid button per state. Start recording when
  stopped, Resume when paused, Write report once the audit length is reached.
  Pause is outlined; Stop and the tools row are quiet text buttons. The
  first-run permissions panel uses outlined buttons and the amber dot for
  "not allowed yet".
- **Shape:** radius 8 on buttons and inputs, 10 on panels. Buttons are 36 px
  tall, the primary 40. Spacing comes in steps of 4, 8, 12, 16 and 20 (20 between
  sections).
- **Icons:** Lucide, inline, 16 px, stroke 1.75. No emojis.
- **Words:** Start recording, Stop recording (Stop where space is short),
  Pause 15 min, Pause 1 hour, Resume, Write report, Open last report, Check in
  now, Summarise now, Open folder, Choose, Use default, Save changes. Use the
  same verb everywhere, the menu bar menu included.
- **States:** skeleton until the first status arrives. The report shows a
  spinner and a sentence while it runs. Empty summaries explain when the
  first one will arrive. Errors from the last hour show as one red alert
  under the status.
- **Theme:** follows the Mac's light/dark appearance through
  `prefers-color-scheme`; the native title bar follows it on its own.
- **App icon:** the character from `make_icon.py`: white round body, dot
  eyes, pink cheeks, red antenna light, on a soft sky-blue tile (205, 226,
  250). The paused menu bar version has an amber light and closed eyes.

## Check-in popup (`checkin.html`)

Its one question: what are you doing right now? The check-in question is the
headline, at 15 (not 22), because Claude's follow-ups run to three or four lines
and the popup should stay small. Solid button: Save answer (off until something
is typed). Skip and Pause 1 hour are quiet. Amber dot on the "Check-in" label
means needs attention. Toasts: "Answer saved", "Capture paused for 1 hour". Kit
files are copies; everything else is in `DESIGN_RULES.md` of the kit.

## Wrap-up popup (`wrapup.html`)

Its one question: how did the day go? Same frame as the check-in, 460 by 600.
Three optional fields (finished off-screen, blockers, first thing tomorrow);
the day's check-in blockers sit under the blocker field as a hint. Solid
button: Write today's log. Not today is quiet. Esc is Not today, Cmd+Enter
writes (Ctrl+Enter too). The check-in's optional blocker field is a second, shorter textarea
under the answer with the label "Anything blocking you?" and "Optional".

## Ukrainian

Every visible word comes from `i18n.js` (pages) or `TEXT` in
`screen_audit.py` (menu bar, file headers). New text gets both languages or it
doesn't ship. Ukrainian avoids plural agreement where it can ("Днів записано:
3 з 7", "Ще 4 дн."), uses "хв" and "год", and keeps the same verb everywhere
(Почати запис, Зупинити запис, Пауза, Продовжити, Написати журнал за сьогодні).
