app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

function extractPrismSource(text) {
  var marker = "PRISM_SOURCE=";
  var index = String(text).indexOf(marker);
  if (index < 0) return null;
  return String(text).substring(index + marker.length).replace(/^\s+|\s+$/g, "");
}

if (app.documents.length === 0) {
  throw new Error("No Illustrator document is open.");
}

var doc = app.activeDocument;
var sourcePath = null;

if (doc.selection.length > 0) {
  sourcePath = extractPrismSource(doc.selection[0].note);
}

if (!sourcePath) {
  for (var i = 0; i < doc.placedItems.length; i++) {
    sourcePath = extractPrismSource(doc.placedItems[i].note);
    if (sourcePath) break;
  }
}

if (!sourcePath) {
  throw new Error("No PRISM_SOURCE note was found in the current Illustrator document.");
}

var sourceFile = new File(sourcePath);
if (!sourceFile.exists) {
  throw new Error("Missing Prism source: " + sourcePath);
}

sourceFile.execute();
$.writeln("opened=" + sourcePath);
