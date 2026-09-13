import { escapeHtml } from '/static/js/html.js';

function ansiColor256ToCss(n) {
  if (n < 0 || n > 255) return '#ffffff';
  if (n < 16) {
    const std = [
      '#000000', '#cd0000', '#00cd00', '#cdcd00', '#0000ee', '#cd00cd', '#00cdcd', '#e5e5e5',
      '#7f7f7f', '#ff0000', '#00ff00', '#ffff00', '#5c5cff', '#ff00ff', '#00ffff', '#ffffff'
    ];
    return std[n];
  }
  if (n >= 232) {
    const g = 8 + (n - 232) * 10;
    return `rgb(${g},${g},${g})`;
  }
  const idx = n - 16;
  const b = (idx % 6) * 51;
  const g = (Math.floor(idx / 6) % 6) * 51;
  const r = Math.floor(idx / 36) * 51;
  return `rgb(${r},${g},${b})`;
}

export function ansiToHtml(text) {
  if (!text) return '';

  let clean = String(text);

  // If text has carriage return, take the overwritten portion
  if (clean.includes('\r')) {
    const parts = clean.split('\r');
    clean = parts[parts.length - 1] || parts[parts.length - 2] || clean;
  }

  // Restore ESC if string contains raw [36m patterns where \u001b was stripped
  if (!clean.includes('\u001b') && /\[[0-9]{1,3}(?:;[0-9]{1,3})*m/.test(clean)) {
    clean = clean.replace(/\[([0-9]{1,3}(?:;[0-9]{1,3})*)m/g, '\u001b[$1m');
  }

  // Strip OSC (title/links) and other non-SGR sequences
  clean = clean
    .replace(/\u001b\][^\u0007\u001b]*(\u0007|\u001b\\)/g, '')
    .replace(/\u001b[@-Z\\-_]/g, '');

  const sgrRegex = /\u001b\[([0-9;]*)([a-zA-Z])/g;
  let lastIndex = 0;
  let match;

  const currentStyles = {
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    fg: null,
    bg: null,
    fgCustom: null,
    bgCustom: null
  };

  function hasActiveStyle() {
    return (
      currentStyles.bold ||
      currentStyles.dim ||
      currentStyles.italic ||
      currentStyles.underline ||
      currentStyles.fg ||
      currentStyles.bg ||
      currentStyles.fgCustom ||
      currentStyles.bgCustom
    );
  }

  function getSpanAttrs() {
    const classes = [];
    const styles = [];

    if (currentStyles.bold) classes.push('ansi-bold');
    if (currentStyles.dim) classes.push('ansi-dim');
    if (currentStyles.italic) classes.push('ansi-italic');
    if (currentStyles.underline) classes.push('ansi-underline');

    if (currentStyles.fg) classes.push('ansi-fg-' + currentStyles.fg);
    if (currentStyles.bg) classes.push('ansi-bg-' + currentStyles.bg);

    if (currentStyles.fgCustom) styles.push('color:' + currentStyles.fgCustom);
    if (currentStyles.bgCustom) styles.push('background-color:' + currentStyles.bgCustom);

    let attr = '';
    if (classes.length) attr += ` class="${classes.join(' ')}"`;
    if (styles.length) attr += ` style="${styles.join(';')}"`;
    return attr;
  }

  let result = '';

  while ((match = sgrRegex.exec(clean)) !== null) {
    const textChunk = clean.slice(lastIndex, match.index);
    if (textChunk) {
      const escaped = escapeHtml(textChunk);
      if (hasActiveStyle()) {
        result += `<span${getSpanAttrs()}>${escaped}</span>`;
      } else {
        result += escaped;
      }
    }
    lastIndex = sgrRegex.lastIndex;

    const command = match[2];
    const paramsStr = match[1];

    if (command !== 'm') {
      // Non-color CSI command, ignore/strip
      continue;
    }

    const params = paramsStr ? paramsStr.split(';').map((p) => parseInt(p, 10)) : [0];

    for (let i = 0; i < params.length; i++) {
      const code = isNaN(params[i]) ? 0 : params[i];

      if (code === 0) {
        currentStyles.bold = false;
        currentStyles.dim = false;
        currentStyles.italic = false;
        currentStyles.underline = false;
        currentStyles.fg = null;
        currentStyles.bg = null;
        currentStyles.fgCustom = null;
        currentStyles.bgCustom = null;
      } else if (code === 1) {
        currentStyles.bold = true;
      } else if (code === 2) {
        currentStyles.dim = true;
      } else if (code === 3) {
        currentStyles.italic = true;
      } else if (code === 4) {
        currentStyles.underline = true;
      } else if (code === 22) {
        currentStyles.bold = false;
        currentStyles.dim = false;
      } else if (code === 23) {
        currentStyles.italic = false;
      } else if (code === 24) {
        currentStyles.underline = false;
      } else if (code >= 30 && code <= 37) {
        const colors = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'];
        currentStyles.fg = colors[code - 30];
        currentStyles.fgCustom = null;
      } else if (code === 39) {
        currentStyles.fg = null;
        currentStyles.fgCustom = null;
      } else if (code >= 40 && code <= 47) {
        const colors = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'];
        currentStyles.bg = colors[code - 40];
        currentStyles.bgCustom = null;
      } else if (code === 49) {
        currentStyles.bg = null;
        currentStyles.bgCustom = null;
      } else if (code >= 90 && code <= 97) {
        const colors = ['bright-black', 'bright-red', 'bright-green', 'bright-yellow', 'bright-blue', 'bright-magenta', 'bright-cyan', 'bright-white'];
        currentStyles.fg = colors[code - 90];
        currentStyles.fgCustom = null;
      } else if (code >= 100 && code <= 107) {
        const colors = ['bright-black', 'bright-red', 'bright-green', 'bright-yellow', 'bright-blue', 'bright-magenta', 'bright-cyan', 'bright-white'];
        currentStyles.bg = colors[code - 100];
        currentStyles.bgCustom = null;
      } else if (code === 38 || code === 48) {
        const isFg = code === 38;
        if (params[i + 1] === 5 && i + 2 < params.length) {
          const colorIdx = params[i + 2];
          i += 2;
          const cssColor = ansiColor256ToCss(colorIdx);
          if (isFg) {
            currentStyles.fg = null;
            currentStyles.fgCustom = cssColor;
          } else {
            currentStyles.bg = null;
            currentStyles.bgCustom = cssColor;
          }
        } else if (params[i + 1] === 2 && i + 4 < params.length) {
          const r = params[i + 2], g = params[i + 3], b = params[i + 4];
          i += 4;
          const cssColor = `rgb(${r},${g},${b})`;
          if (isFg) {
            currentStyles.fg = null;
            currentStyles.fgCustom = cssColor;
          } else {
            currentStyles.bg = null;
            currentStyles.bgCustom = cssColor;
          }
        }
      }
    }
  }

  // Trailing text
  const remaining = clean.slice(lastIndex);
  if (remaining) {
    const escaped = escapeHtml(remaining);
    if (hasActiveStyle()) {
      result += `<span${getSpanAttrs()}>${escaped}</span>`;
    } else {
      result += escaped;
    }
  }

  // Clean up any remaining unprintable control characters (except tab and newline)
  return result.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '');
}
