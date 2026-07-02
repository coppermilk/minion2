// kindle bot: Google Doc -> PDF -> print/; weekly archive
// (BLUEPRINT 9, the declared off-kernel outlier).

// Consumes only Google Docs from _inbox/ -- the shared-folder rule
// of BLUEPRINT 1.2: the sorter owns image extensions, this bot owns
// Docs; neither writes the other's type.
function convertInbox() {
  var inbox = DriveApp.getFolderById(CONFIG.inboxFolderId);
  var print = DriveApp.getFolderById(CONFIG.printFolderId);
  var docs = inbox.getFilesByType(MimeType.GOOGLE_DOCS);
  while (docs.hasNext()) {
    var doc = docs.next();
    var pdf = doc.getAs(MimeType.PDF);
    print.createFile(pdf).setName(doc.getName() + '.pdf');
  }
}

// Weekly: archive consumed Docs into Scripts/ as done__<name>.
function weeklyArchive() {
  var inbox = DriveApp.getFolderById(CONFIG.inboxFolderId);
  var scripts = DriveApp.getFolderById(CONFIG.scriptsFolderId);
  var docs = inbox.getFilesByType(MimeType.GOOGLE_DOCS);
  while (docs.hasNext()) {
    var doc = docs.next();
    doc.setName(CONFIG.donePrefix + doc.getName());
    doc.moveTo(scripts);
  }
}
