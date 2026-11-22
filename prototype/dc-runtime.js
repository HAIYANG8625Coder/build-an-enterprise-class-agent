// A tiny runtime for the prototype pages. Each page holds a template (with
// {{holes}}, <sc-if> and <sc-for>) and a Component class whose renderVals()
// returns the values. Clicking a control calls a handler that may setState(),
// and the page renders again. Plain browser JavaScript, no dependencies.
(function () {
  var handlers = [];
  var root, template, component;

  function DCLogic(props) {
    this.props = props || {};
    this.state = {};
  }
  DCLogic.prototype.setState = function (patch) {
    var p = typeof patch === 'function' ? patch(this.state) : patch;
    this.state = Object.assign({}, this.state, p);
    draw();
  };
  DCLogic.prototype.forceUpdate = function () { draw(); };
  window.DCLogic = DCLogic;

  function lookup(path, scopes) {
    path = path.trim();
    if (path === 'true') return true;
    if (path === 'false') return false;
    if (/^-?\d+(\.\d+)?$/.test(path)) return Number(path);
    var parts = path.split('.');
    for (var i = scopes.length - 1; i >= 0; i--) {
      if (Object.prototype.hasOwnProperty.call(scopes[i], parts[0])) {
        var v = scopes[i][parts[0]];
        for (var k = 1; k < parts.length; k++) v = v == null ? undefined : v[parts[k]];
        return v;
      }
    }
    return undefined;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function attr(openTag, name) {
    var m = openTag.match(new RegExp(name + '="([^"]*)"'));
    return m ? m[1] : null;
  }

  function hole(text, scopes) {
    var m = text && text.match(/^\{\{\s*([^}]+?)\s*\}\}$/);
    return m ? lookup(m[1], scopes) : text;
  }

  function findClose(tpl, tag, from) {
    var re = new RegExp('<(/?)' + tag + '\\b[^>]*>', 'g'), depth = 1, m;
    re.lastIndex = from;
    while ((m = re.exec(tpl))) {
      if (m[1]) { if (--depth === 0) return { start: m.index, end: re.lastIndex }; }
      else depth++;
    }
    throw new Error('unclosed <' + tag + '>');
  }

  function fill(text, scopes) {
    text = text.replace(/\son([A-Z][a-zA-Z]*)="\{\{\s*([^}]+?)\s*\}\}"/g, function (_, ev, path) {
      var fn = lookup(path, scopes);
      if (typeof fn !== 'function') return '';
      handlers.push(fn);
      return ' data-dc-' + ev.toLowerCase() + '="' + (handlers.length - 1) + '"';
    });
    return text.replace(/\{\{\s*([^}]+?)\s*\}\}/g, function (_, path) {
      var v = lookup(path, scopes);
      return v == null ? '' : esc(v);
    });
  }

  function render(tpl, scopes) {
    var out = '', at = 0, re = /<sc-(if|for)\b[^>]*>/g, m;
    while ((m = re.exec(tpl))) {
      out += fill(tpl.slice(at, m.index), scopes);
      var tag = 'sc-' + m[1], close = findClose(tpl, tag, re.lastIndex);
      var inner = tpl.slice(re.lastIndex, close.start);
      if (m[1] === 'if') {
        if (hole(attr(m[0], 'value'), scopes)) out += render(inner, scopes);
      } else {
        var list = hole(attr(m[0], 'list'), scopes) || [], as = attr(m[0], 'as');
        for (var i = 0; i < list.length; i++) {
          var scope = { $index: i };
          scope[as] = list[i];
          out += render(inner, scopes.concat([scope]));
        }
      }
      at = re.lastIndex = close.end;
    }
    return out + fill(tpl.slice(at), scopes);
  }

  function draw() {
    handlers = [];
    root.innerHTML = render(template, [component.renderVals()]);
  }

  window.DC = {
    mount: function (Component) {
      root = document.getElementById('app');
      template = document.getElementById('dc-template').textContent;
      component = new Component({});
      root.addEventListener('click', function (e) {
        var el = e.target.closest('[data-dc-click]');
        if (!el) return;
        e.preventDefault();
        handlers[Number(el.getAttribute('data-dc-click'))]();
      });
      draw();
    }
  };
})();
