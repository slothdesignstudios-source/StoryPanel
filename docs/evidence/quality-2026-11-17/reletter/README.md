# Re-lettering the two real exported pages (no art regenerated, no provider calls)

Every image is stamped at the top: **AUTOMATIC** (faces guessed from the script, as in the export) or
**MANUAL** (face positions typed in by hand). MANUAL images show what the letterer does when faces are
known; they are **not** output of the automatic pipeline. `*-reading-size.png` are 1000 px wide (normal
reading size). `report.json` also measures, per balloon, how far its tail ends from the speaker's face
and from the nearest other face.

- `train-before-after.jpg`, `tulips-before-after.jpg`: left, your exported page as delivered; right, the
  same artwork re-lettered by the current code with the face positions in `annotations.json`.
- `*-guessed.jpg`: the same art re-lettered with faces guessed from the script, as happened in the export;
  placement is unchanged, but it is now flagged (`report.json`).
- `*-located.jpg`: with located faces.
- `report.json`: the flags and the export warnings the app would show for each version.
- `reletter.py` and `annotations.json`: how to reproduce it from your own PDFs (`pdfimages -png comic.pdf art`).

The face positions in `annotations.json` were read off the art by eye. They stand in for the panel
check's head boxes, which are in your database, not in the PDF. They show what the letterer does when it
knows where faces are; they are not the check's output.
