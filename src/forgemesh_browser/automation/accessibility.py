"""DOM / accessibility inspection extracting stable element references."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

SNAPSHOT_JS = r"""
([limit, interactiveOnly]) => {
  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'search') return 'searchbox';
      if (type === 'range') return 'slider';
      if (type === 'file') return 'file-input';
      return 'textbox';
    }
    return null;
  };
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      parseFloat(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
  };
  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const labelEl = document.getElementById(labelledby);
      if (labelEl) return labelEl.innerText.trim();
    }
    if (el.labels && el.labels.length) {
      const text = Array.from(el.labels).map(l => l.innerText).join(' ').trim();
      if (text) return text;
    }
    if (el.tagName.toLowerCase() === 'img') return (el.getAttribute('alt') || '').trim();
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    const title = el.getAttribute('title');
    if (title) return title.trim();
    const text = (el.innerText || '').trim();
    if (text) return text.slice(0, 120);
    return '';
  };

  let seq = window.__fmbSeq || 0;
  const interactiveSelector = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
    '[role]', '[contenteditable="true"]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const extendedSelector = interactiveSelector + ',h1,h2,h3,h4,p,img,label,form';
  const selector = interactiveOnly ? interactiveSelector : extendedSelector;
  const elements = [];
  for (const el of document.querySelectorAll(selector)) {
    if (elements.length >= limit) break;
    if (!visible(el)) continue;
    let ref = el.getAttribute('data-fmb-ref');
    if (!ref) {
      ref = 'e' + (++seq);
      el.setAttribute('data-fmb-ref', ref);
    }
    const rect = el.getBoundingClientRect();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || implicitRole(el);
    elements.push({
      ref,
      tag,
      role,
      name: accessibleName(el),
      text: (el.innerText || '').trim().slice(0, 160),
      id: el.id || null,
      name_attr: el.getAttribute('name') || null,
      type: type || null,
      placeholder: el.getAttribute('placeholder') || null,
      href: el.getAttribute('href') || null,
      has_value: 'value' in el ? Boolean(el.value) : false,
      disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
      checked: 'checked' in el ? Boolean(el.checked) : null,
      visible: true,
      bounds: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    });
  }
  window.__fmbSeq = seq;
  return {
    url: location.href,
    title: document.title,
    scroll: { y: window.scrollY, height: document.documentElement.scrollHeight },
    elements
  };
}
"""

EXTRACT_JS = r"""
() => {
  const text = (el) => (el && el.innerText ? el.innerText.trim() : '');
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4'))
    .slice(0, 200)
    .map(h => ({ level: Number(h.tagName.slice(1)), text: text(h).slice(0, 300) }))
    .filter(h => h.text);
  const links = Array.from(document.querySelectorAll('a[href]'))
    .slice(0, 1000)
    .map(a => ({ text: text(a).slice(0, 200), href: a.href }))
    .filter(l => l.href);
  const images = Array.from(document.querySelectorAll('img'))
    .slice(0, 300)
    .map(img => ({
      src: img.currentSrc || img.src,
      alt: (img.alt || '').slice(0, 200),
      width: img.naturalWidth,
      height: img.naturalHeight
    }));
  const forms = Array.from(document.forms).map(form => ({
    action: form.action || null,
    method: (form.method || 'get').toLowerCase(),
    fields: Array.from(form.elements)
      .filter(el => el.name)
      .map(el => ({
        name: el.name,
        type: (el.type || '').toLowerCase(),
        label: el.labels && el.labels.length ? text(el.labels[0]).slice(0, 120) : null,
        required: Boolean(el.required)
      }))
  }));
  const meta = (name) => {
    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
    return el ? el.getAttribute('content') : null;
  };
  return {
    url: location.href,
    title: document.title,
    description: meta('description') || meta('og:description'),
    canonical: document.querySelector('link[rel="canonical"]')?.href || null,
    lang: document.documentElement.lang || null,
    text: (document.body ? document.body.innerText : '').slice(0, 200000),
    headings,
    links,
    images,
    forms
  };
}
"""


async def snapshot(
    page: Page, *, interactive_only: bool = True, limit: int = 300
) -> dict[str, Any]:
    return await page.evaluate(SNAPSHOT_JS, [limit, interactive_only])


async def extract(page: Page) -> dict[str, Any]:
    return await page.evaluate(EXTRACT_JS)
