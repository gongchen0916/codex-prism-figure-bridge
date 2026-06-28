app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

// Configure these paths for your project before running this script.
var linkedPath = "/ABSOLUTE/PATH/TO/linked_prism_export.pdf";
var aiPath = "/ABSOLUTE/PATH/TO/linked_prism_figure.ai";
var linkedFile = new File(linkedPath);
if (!linkedFile.exists) {
  throw new Error("Missing linked file: " + linkedPath);
}

if (app.documents.length === 0) {
  app.open(new File(aiPath));
}

var doc = app.activeDocument;
var refreshed = 0;
for (var i = 0; i < doc.placedItems.length; i++) {
  var item = doc.placedItems[i];
  if (item.name === "PRISM_LINKED_PDF__3Hz_right" || String(item.note).indexOf("PRISM_SOURCE=") >= 0) {
    item.file = linkedFile;
    refreshed += 1;
  }
}
app.redraw();
doc.save();

$.writeln("refreshed=" + refreshed);
