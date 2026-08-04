/* Zoom / pan / fullscreen controls for rendered mermaid diagrams.
   Shared by the GitHub Pages viewer and the web app docs view.

   Call CodeWikiDiagrams.enhanceAll() after mermaid has rendered. It is
   idempotent, so single-page navigation can call it on every render.

   No external dependencies: the GitHub Pages export is a single self-contained
   file, so this must not pull in a pan/zoom library.

   NOTE: the web app embeds this inside a Jinja2 template, so the source must
   never contain a doubled curly brace or a brace-percent pair. */

(function () {
    'use strict';

    var MIN_SCALE = 0.2;
    var MAX_SCALE = 8;
    var STEP = 1.25;

    function make(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) {
            node.className = cls;
        }
        if (text !== undefined && text !== null) {
            node.textContent = text;
        }
        return node;
    }

    function makeButton(label, title, action) {
        var btn = make('button', 'cw-diagram-btn', label);
        btn.type = 'button';
        btn.title = title;
        btn.setAttribute('aria-label', title);
        btn.setAttribute('data-cw-action', action);
        return btn;
    }

    /* Mermaid emits a viewBox, which gives a layout-independent natural size.
       Falling back to the measured box keeps this working if that changes. */
    function naturalSize(svg) {
        var box = svg.viewBox && svg.viewBox.baseVal;
        if (box && box.width > 0 && box.height > 0) {
            return { w: box.width, h: box.height };
        }
        var rect = svg.getBoundingClientRect();
        return { w: rect.width || 600, h: rect.height || 400 };
    }

    function enhanceDiagram(host) {
        if (!host || host.getAttribute('data-cw-enhanced') === '1') {
            return;
        }
        var svg = host.querySelector('svg');
        if (!svg) {
            return;  // render failed - leave the error message visible
        }

        var size = naturalSize(svg);
        svg.style.maxWidth = 'none';
        svg.setAttribute('width', size.w);
        svg.setAttribute('height', size.h);

        var viewport = make('div', 'cw-diagram-viewport');
        var canvas = make('div', 'cw-diagram-canvas');
        canvas.appendChild(svg);
        viewport.appendChild(canvas);
        viewport.tabIndex = 0;
        viewport.setAttribute('role', 'group');
        viewport.setAttribute(
            'aria-label',
            'Diagram. Drag to pan, Ctrl and scroll to zoom, plus and minus keys to zoom, 0 to reset.'
        );

        var level = make('span', 'cw-diagram-level', '100%');
        var zoomOut = makeButton('−', 'Zoom out', 'out');
        var zoomIn = makeButton('+', 'Zoom in', 'in');
        var reset = makeButton('↻', 'Reset zoom', 'reset');
        var full = makeButton('⛶', 'Fullscreen', 'full');

        var toolbar = make('div', 'cw-diagram-toolbar');
        toolbar.appendChild(zoomOut);
        toolbar.appendChild(level);
        toolbar.appendChild(zoomIn);
        toolbar.appendChild(make('span', 'cw-diagram-sep'));
        toolbar.appendChild(reset);
        toolbar.appendChild(full);

        host.innerHTML = '';
        host.appendChild(viewport);
        host.appendChild(toolbar);
        host.setAttribute('data-cw-enhanced', '1');

        var scale = 1;
        var tx = 0;
        var ty = 0;
        var userAdjusted = false;

        function clamp(value) {
            return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
        }

        function inFullscreen() {
            return document.fullscreenElement === host ||
                host.classList.contains('cw-diagram-pseudo-full');
        }

        function apply() {
            canvas.style.transform =
                'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
            level.textContent = Math.round(scale * 100) + '%';
            zoomOut.disabled = scale <= MIN_SCALE + 0.0001;
            zoomIn.disabled = scale >= MAX_SCALE - 0.0001;
        }

        function autoHeight() {
            if (inFullscreen()) {
                // Drop the inline height so the stylesheet's 100vh rule applies.
                // An inline height outranks it and would pin fullscreen to the
                // small inline size.
                viewport.style.height = '';
                return;
            }
            var cap = Math.round(window.innerHeight * 0.75);
            var natural = Math.round(size.h * scale);
            viewport.style.height = Math.max(120, Math.min(natural, cap)) + 'px';
        }

        /* Scale to fit the width, never magnifying a diagram that already
           fits - that keeps first paint identical to the old behaviour. */
        function fit() {
            var width = viewport.clientWidth || host.clientWidth || 600;
            var next = width / size.w;
            if (next > 1) {
                next = 1;
            }
            scale = clamp(next);
            tx = Math.max(0, (width - size.w * scale) / 2);
            ty = 0;
            autoHeight();
            apply();
        }

        function zoomTo(next, anchorX, anchorY) {
            var from = scale;
            var to = clamp(next);
            if (to === from) {
                return;
            }
            if (anchorX === undefined || anchorX === null) {
                anchorX = viewport.clientWidth / 2;
                anchorY = viewport.clientHeight / 2;
            }
            tx = anchorX - (anchorX - tx) * (to / from);
            ty = anchorY - (anchorY - ty) * (to / from);
            scale = to;
            userAdjusted = true;
            apply();
        }

        function afterFullscreenChange() {
            full.title = inFullscreen() ? 'Exit fullscreen' : 'Fullscreen';
            full.setAttribute('aria-label', full.title);
            full.textContent = inFullscreen() ? '✕' : '⛶';
            userAdjusted = false;
            fit();  // recomputes the viewport height for the new mode
        }

        function usePseudoFullscreen() {
            host.classList.add('cw-diagram-pseudo-full');
            afterFullscreenChange();
        }

        function toggleFullscreen() {
            if (inFullscreen()) {
                if (document.fullscreenElement === host && document.exitFullscreen) {
                    document.exitFullscreen();
                } else {
                    host.classList.remove('cw-diagram-pseudo-full');
                    afterFullscreenChange();
                }
                return;
            }
            var request = host.requestFullscreen || host.webkitRequestFullscreen;
            if (!request) {
                usePseudoFullscreen();
                return;
            }
            var result;
            try {
                result = request.call(host);
            } catch (err) {
                usePseudoFullscreen();
                return;
            }
            if (result && typeof result.then === 'function') {
                result.then(afterFullscreenChange, usePseudoFullscreen);
            } else {
                afterFullscreenChange();
            }
        }

        toolbar.addEventListener('click', function (event) {
            var btn = event.target.closest ? event.target.closest('.cw-diagram-btn') : null;
            if (!btn) {
                return;
            }
            var action = btn.getAttribute('data-cw-action');
            if (action === 'in') {
                zoomTo(scale * STEP);
            } else if (action === 'out') {
                zoomTo(scale / STEP);
            } else if (action === 'reset') {
                userAdjusted = false;
                fit();
            } else if (action === 'full') {
                toggleFullscreen();
            }
        });

        /* Ctrl/Cmd is required so the page still scrolls normally over a diagram. */
        viewport.addEventListener('wheel', function (event) {
            if (!event.ctrlKey && !event.metaKey) {
                return;
            }
            event.preventDefault();
            var rect = viewport.getBoundingClientRect();
            var factor = event.deltaY < 0 ? STEP : 1 / STEP;
            zoomTo(scale * factor, event.clientX - rect.left, event.clientY - rect.top);
        }, { passive: false });

        var dragging = false;
        var lastX = 0;
        var lastY = 0;
        var pointerId = null;

        viewport.addEventListener('pointerdown', function (event) {
            if (event.button !== 0) {
                return;
            }
            dragging = true;
            lastX = event.clientX;
            lastY = event.clientY;
            pointerId = event.pointerId;
            if (viewport.setPointerCapture) {
                viewport.setPointerCapture(pointerId);
            }
            viewport.classList.add('cw-panning');
        });

        viewport.addEventListener('pointermove', function (event) {
            if (!dragging) {
                return;
            }
            tx += event.clientX - lastX;
            ty += event.clientY - lastY;
            lastX = event.clientX;
            lastY = event.clientY;
            userAdjusted = true;
            apply();
        });

        function endDrag() {
            if (!dragging) {
                return;
            }
            dragging = false;
            viewport.classList.remove('cw-panning');
            if (pointerId !== null && viewport.releasePointerCapture) {
                try {
                    viewport.releasePointerCapture(pointerId);
                } catch (err) {
                    /* pointer already released */
                }
            }
            pointerId = null;
        }

        viewport.addEventListener('pointerup', endDrag);
        viewport.addEventListener('pointercancel', endDrag);
        viewport.addEventListener('dblclick', function () {
            userAdjusted = false;
            fit();
        });

        viewport.addEventListener('keydown', function (event) {
            if (event.key === '+' || event.key === '=') {
                event.preventDefault();
                zoomTo(scale * STEP);
            } else if (event.key === '-' || event.key === '_') {
                event.preventDefault();
                zoomTo(scale / STEP);
            } else if (event.key === '0') {
                event.preventDefault();
                userAdjusted = false;
                fit();
            } else if (event.key === 'f' || event.key === 'F') {
                event.preventDefault();
                toggleFullscreen();
            } else if (event.key === 'Escape' && host.classList.contains('cw-diagram-pseudo-full')) {
                host.classList.remove('cw-diagram-pseudo-full');
                afterFullscreenChange();
            }
        });

        document.addEventListener('fullscreenchange', function () {
            if (document.fullscreenElement === host || host.getAttribute('data-cw-was-full') === '1') {
                host.setAttribute('data-cw-was-full', document.fullscreenElement === host ? '1' : '0');
                afterFullscreenChange();
            }
        });

        window.addEventListener('resize', function () {
            if (!userAdjusted) {
                fit();
            }
        });

        fit();
    }

    function enhanceAll(root) {
        var scope = root || document;
        var nodes = scope.querySelectorAll('.mermaid');
        for (var i = 0; i < nodes.length; i++) {
            enhanceDiagram(nodes[i]);
        }
    }

    window.CodeWikiDiagrams = { enhanceAll: enhanceAll, enhanceDiagram: enhanceDiagram };
})();
