app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

// Configure these paths for your project before running this script.
var linkedPath = "/ABSOLUTE/PATH/TO/linked_prism_export.pdf";
var aiPath = "/ABSOLUTE/PATH/TO/linked_prism_figure.ai";
var prismSourcePath = "/ABSOLUTE/PATH/TO/source_graph.pzfx";

var linkedFile = new File(linkedPath);
if (!linkedFile.exists) {
  throw new Error("Missing linked file: " + linkedPath);
}

var doc = app.documents.add(DocumentColorSpace.RGB, 442, 382);
doc.rulerUnits = RulerUnits.Points;
doc.pageItems.removeAll();

var placed = doc.placedItems.add();
placed.file = linkedFile;
placed.name = "PRISM_LINKED_PDF__3Hz_right";
placed.note = "PRISM_SOURCE=" + prismSourcePath;
placed.uRL = "prismbridge://open/?path=" + prismSourcePath;

// Center the linked Prism export on the artboard without embedding it.
placed.left = 20;
placed.top = 360;

var opts = new IllustratorSaveOptions();
opts.pdfCompatible = true;
opts.compressed = true;
doc.saveAs(new File(aiPath), opts);

$.writeln("created=" + aiPath);
$.writeln("linked=" + placed.file.fsName);
