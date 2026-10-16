// 180: shared persona selector, escaped Markdown preview and source editor.
// No operator HTML is trusted. Only the tags below are generated; links have a protocol allowlist.
var PERSONA_INDENT = '  ';

function personaInline(text) {
  var tokens = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|\[([^\]\n]+)\]\(([^\s)]+)\)/g;
  var out = '';
  var end = 0;
  var match;
  while ((match = tokens.exec(text))) {
    out += esc(text.slice(end, match.index));
    if (match[1] !== undefined) out += '<code>' + esc(match[1]) + '</code>';
    else if (match[2] !== undefined) out += '<strong>' + esc(match[2]) + '</strong>';
    else if (match[3] !== undefined) out += '<em>' + esc(match[3]) + '</em>';
    else if (/^(https?:\/\/|mailto:)/i.test(match[5]) && !/[\u0000-\u0020\u007f]/.test(match[5])) {
      out += '<a href="' + esc(match[5]) + '" rel="noopener noreferrer">' + esc(match[4]) + '</a>';
    } else out += esc(match[0]);
    end = tokens.lastIndex;
  }
  return out + esc(text.slice(end));
}

function personaMarkdown(text) {
  var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  var out = '';
  var paragraph = [];
  var list = '';
  var fence = '';
  var code = [];
  function flushParagraph() {
    if (paragraph.length) out += '<p>' + personaInline(paragraph.join('\n')) + '</p>';
    paragraph = [];
  }
  function closeList() { if (list) out += '</' + esc(list) + '>'; list = ''; }
  lines.forEach(function(line) {
    var marker = /^\s*(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (marker && marker[1].charAt(0) === fence.charAt(0) && marker[1].length >= fence.length) {
        out += '<pre><code>' + esc(code.join('\n')) + '</code></pre>';
        fence = ''; code = [];
      } else code.push(line);
      return;
    }
    if (marker) { flushParagraph(); closeList(); fence = marker[1]; return; }
    var heading = /^\s{0,3}(#{1,6})\s+(.+)$/.exec(line);
    var item = /^\s*(?:([-+*])|\d+[.)])\s+(.+)$/.exec(line);
    var quote = /^\s*>\s?(.*)$/.exec(line);
    if (!line.trim() || heading || item || quote || /^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushParagraph();
      if (!item) closeList();
      if (heading) {
        var tag = 'h' + heading[1].length;
        out += '<' + esc(tag) + '>' + personaInline(heading[2]) + '</' + esc(tag) + '>';
      } else if (item) {
        var kind = item[1] ? 'ul' : 'ol';
        if (kind !== list) { closeList(); list = kind; out += '<' + esc(kind) + '>'; }
        out += '<li>' + personaInline(item[2]) + '</li>';
      } else if (quote) out += '<blockquote>' + personaInline(quote[1]) + '</blockquote>';
      else if (line.trim()) out += '<hr>';
    } else { closeList(); paragraph.push(line); }
  });
  flushParagraph(); closeList();
  if (fence) out += '<pre><code>' + esc(code.join('\n')) + '</code></pre>';
  return out;
}

function personaHighlight(text) {
  return String(text).split('\n').map(function(line) {
    if (/^\s*#/.test(line)) return '<span class="md-heading">' + esc(line) + '</span>';
    var tokens = /(`[^`]*`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]*\)|^\s*[-+*]\s|^\s*`{3,}.*$)/g;
    var end = 0;
    var out = '';
    var match;
    while ((match = tokens.exec(line))) {
      out += esc(line.slice(end, match.index)) + '<span class="md-token">' + esc(match[0]) + '</span>';
      end = tokens.lastIndex;
    }
    return out + esc(line.slice(end));
  }).join('\n') + '\n';
}

// Textareas normalize line endings on assignment. Keep the exact source when the displayed value
// has not changed, including after conflict reapply. No renderer output ever goes into the source.
function personaSourceValue(field) {
  if (field.value === field.getAttribute('data-md-loaded')) {
    return JSON.parse(field.getAttribute('data-md-original'));
  }
  return field.value;
}

function personaSetSource(field, text) {
  field.value = text;
  field.setAttribute('data-md-original', JSON.stringify(text));
  field.setAttribute('data-md-loaded', field.value);
}

function personaEditor(text, label) {
  return '<div class="persona-editor" data-md-editor data-md-mode="write">'
    + '<div class="persona-toolbar" role="group" aria-label="Markdown tools">'
    + [['heading', 'Heading'], ['bold', 'Bold'], ['list', 'List'], ['code', 'Code'], ['link', 'Link']].map(function(tool) {
      return '<button type="button" class="cfg-btn" data-md-tool="' + esc(tool[0]) + '">' + esc(tool[1]) + '</button>';
    }).join('')
    + '<span class="persona-view-tools"><button type="button" class="cfg-btn" data-md-view="write" aria-pressed="true">Write</button>'
    + '<button type="button" class="cfg-btn" data-md-view="preview" aria-pressed="false">Preview</button></span></div>'
    + '<p class="muted persona-editor-help">Markdown source is saved exactly as typed. Tab indents; Shift+Tab outdents; Escape then Tab leaves the source. Ctrl/Cmd+S reviews a save.</p>'
    + '<div class="persona-panes"><div class="persona-source" data-md-write>'
    + '<pre data-md-highlight aria-hidden="true"></pre>'
    + '<textarea data-md-source data-field="instructions" data-label="Instructions" data-type="text"'
    + ' aria-label="' + esc(label) + '" spellcheck="false" wrap="off" data-orig="' + esc(JSON.stringify(text)) + '"></textarea></div>'
    + '<div class="persona-markdown" data-md-preview role="region" aria-label="Markdown preview" tabindex="0"></div></div></div>';
}

// A pure text transformation, shared by toolbar and indentation. Selection remains inside wrappers.
function personaEdit(text, start, end, action) {
  var before = text.slice(0, start);
  var selection = text.slice(start, end);
  var after = text.slice(end);
  if (action === 'indent' && start === end) {
    return {text: before + PERSONA_INDENT + after, start: start + PERSONA_INDENT.length, end: end + PERSONA_INDENT.length};
  }
  if (action === 'indent' || action === 'outdent' || action === 'heading' || action === 'list') {
    var lineStart = start ? text.lastIndexOf('\n', start - 1) + 1 : 0;
    var lastSelected = end > start && text.charAt(end - 1) === '\n' ? end - 1 : end;
    var lineEnd = text.indexOf('\n', lastSelected);
    if (lineEnd < 0) lineEnd = text.length;
    var block = text.slice(lineStart, lineEnd);
    var prefix = action === 'heading' ? '## ' : action === 'list' ? '- ' : PERSONA_INDENT;
    var offset = lineStart;
    var newStart = start;
    var newEnd = end;
    var changed = block.split('\n').map(function(line) {
      var replacement = action === 'outdent' ? line.replace(/^(  |\t| )/, '') : prefix + line;
      var removed = line.length - replacement.length;
      if (offset <= start) newStart -= removed > 0 ? Math.min(removed, start - offset) : removed;
      if (offset <= end) newEnd -= removed > 0 ? Math.min(removed, end - offset) : removed;
      offset += line.length + 1;
      return replacement;
    }).join('\n');
    return {text: text.slice(0, lineStart) + changed + text.slice(lineEnd), start: newStart, end: newEnd};
  }
  var wrap = action === 'bold' ? '**' : action === 'code' ? '`' : '[';
  var suffix = action === 'link' ? '](https://example.com)' : wrap;
  return {text: before + wrap + selection + suffix + after, start: start + wrap.length, end: end + wrap.length};
}

function personaHashRole(context) {
  var parts = (location.hash || '').replace(/^#/, '').split('/');
  try { return decodeURIComponent(context === 'config' ? parts[1] || '' : parts[0]); }
  catch(e) { return ''; }
}

function personaSelector(personas, context) {
  return '<div class="persona-rail" role="tablist" aria-label="Personas">' + personas.map(function(p) {
    var role = String(p.role || '');
    var id = context + '-persona-' + role;
    return '<button type="button" class="persona-tab" role="tab" data-persona-tab="' + esc(role) + '"'
      + ' id="' + esc(id + '-tab') + '" aria-controls="' + esc(id) + '" aria-selected="false" tabindex="-1">'
      + agentLabel(role) + personaStageNote(role)
      // The read-only page cannot have unsaved text, so only the editor carries the marker.
      + (context === 'read' ? '' : '<span data-persona-dirty hidden> · Unsaved</span>') + '</button>';
  }).join('') + '</div>';
}

function personaPanel(role, context, html) {
  var id = context + '-persona-' + role;
  return '<div role="tabpanel" data-persona-panel="' + esc(role) + '" id="' + esc(id) + '"'
    + ' aria-labelledby="' + esc(id + '-tab') + '" aria-hidden="true" hidden>' + html + '</div>';
}

function personaSyncPanels(root) {
  var selected = root.querySelector('[data-persona-tab][aria-selected="true"]');
  var role = selected && selected.getAttribute('data-persona-tab');
  var ancestorHidden = !!root.closest('[hidden]');
  root.querySelectorAll('[data-persona-panel]').forEach(function(panel) {
    panel.hidden = ancestorHidden || panel.getAttribute('data-persona-panel') !== role;
    panel.setAttribute('aria-hidden', panel.hidden ? 'true' : 'false');
  });
}

function personaSelect(root, role, updateUrl) {
  var tabs = root.querySelectorAll('[data-persona-tab]');
  var exists = Array.prototype.some.call(tabs, function(tab) { return tab.getAttribute('data-persona-tab') === role; });
  if (!exists && tabs.length) role = tabs[0].getAttribute('data-persona-tab');
  tabs.forEach(function(tab) {
    var selected = tab.getAttribute('data-persona-tab') === role;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.setAttribute('tabindex', selected ? '0' : '-1');
  });
  personaSyncPanels(root);
  if (typeof root.onPersonaSelect === 'function') root.onPersonaSelect(role);
  if (role && updateUrl && personaHashRole(root.getAttribute('data-persona-context')) !== role) {
    var context = root.getAttribute('data-persona-context');
    var hash = context === 'config' ? '#personas/' : '#';
    pushRoute(pathWithSymphony((context === 'config' ? '/config' : '/personas') + hash + encodeURIComponent(role)));
  }
}

function personaRefresh(root) {
  root.querySelectorAll('[data-md-editor]').forEach(function(editor) {
    var field = editor.querySelector('[data-md-source]');
    var pre = editor.querySelector('[data-md-highlight]');
    pre.innerHTML = personaHighlight(field.value);
    pre.scrollTop = field.scrollTop; pre.scrollLeft = field.scrollLeft;
    editor.querySelector('[data-md-preview]').innerHTML = personaMarkdown(personaSourceValue(field));
  });
  root.querySelectorAll('[data-persona-tab]').forEach(function(tab) {
    var panel = document.getElementById(tab.getAttribute('aria-controls'));
    var field = panel && panel.querySelector('[data-md-source]');
    var dirty = field && personaSourceValue(field) !== JSON.parse(field.getAttribute('data-orig'));
    var marker = tab.querySelector('[data-persona-dirty]');
    if (marker) marker.hidden = !dirty;
  });
}

function personaApplyTool(field, action) {
  var edit = personaEdit(field.value, field.selectionStart, field.selectionEnd, action);
  field.value = edit.text;
  field.focus();
  field.setSelectionRange(edit.start, edit.end);
  personaRefresh(field.closest('[data-persona-context]'));
}

// Mount on an owned root. #181 can supply its own save action and field metadata around this editor.
function personaMount(root, save) {
  if (!root) return;
  root.querySelectorAll('[data-md-source]').forEach(function(field) {
    personaSetSource(field, JSON.parse(field.getAttribute('data-orig')));
    field.addEventListener('scroll', function() {
      var pre = field.parentNode.querySelector('[data-md-highlight]');
      pre.scrollTop = field.scrollTop; pre.scrollLeft = field.scrollLeft;
    });
  });
  personaSelect(root, personaHashRole(root.getAttribute('data-persona-context')), false);
  personaRefresh(root);
  root.addEventListener('input', function() { personaRefresh(root); });
  root.addEventListener('click', function(event) {
    var tab = event.target.closest('[data-persona-tab]');
    if (tab) { personaSelect(root, tab.getAttribute('data-persona-tab'), true); return; }
    var tool = event.target.closest('[data-md-tool], [data-md-view]');
    if (!tool) return;
    var editor = tool.closest('[data-md-editor]');
    if (tool.hasAttribute('data-md-tool')) personaApplyTool(editor.querySelector('[data-md-source]'), tool.getAttribute('data-md-tool'));
    else {
      var mode = tool.getAttribute('data-md-view');
      editor.setAttribute('data-md-mode', mode);
      editor.querySelectorAll('[data-md-view]').forEach(function(button) {
        button.setAttribute('aria-pressed', button.getAttribute('data-md-view') === mode ? 'true' : 'false');
      });
    }
  });
  root.addEventListener('keydown', function(event) {
    var tab = event.target.closest('[data-persona-tab]');
    if (tab && ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(event.key) >= 0) {
      event.preventDefault();
      var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-persona-tab]'));
      var index = tabs.indexOf(tab);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else index = (index + (event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1) + tabs.length) % tabs.length;
      personaSelect(root, tabs[index].getAttribute('data-persona-tab'), true);
      tabs[index].focus();
      return;
    }
    var editor = event.target.closest('[data-md-editor]');
    if (!editor) return;
    var field = editor.querySelector('[data-md-source]');
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      if (!field.disabled && save) save(field);
    } else if (event.target === field && event.key === 'Home' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      // Home scrolls without moving the caret on some platforms. Give source editing consistent line navigation.
      event.preventDefault();
      var backward = field.selectionDirection === 'backward';
      var caret = backward ? field.selectionStart : field.selectionEnd;
      var lineStart = caret ? field.value.lastIndexOf('\n', caret - 1) + 1 : 0;
      var anchor = event.shiftKey ? (backward ? field.selectionEnd : field.selectionStart) : lineStart;
      field.setSelectionRange(Math.min(anchor, lineStart), Math.max(anchor, lineStart), lineStart < anchor ? 'backward' : 'forward');
      field.removeAttribute('data-md-escape');
    } else if (event.target === field && event.key === 'Escape') field.setAttribute('data-md-escape', 'true');
    else if (event.target === field && event.key === 'Tab') {
      if (field.hasAttribute('data-md-escape')) { field.removeAttribute('data-md-escape'); return; }
      event.preventDefault();
      personaApplyTool(field, event.shiftKey ? 'outdent' : 'indent');
    } else field.removeAttribute('data-md-escape');
  });
}
