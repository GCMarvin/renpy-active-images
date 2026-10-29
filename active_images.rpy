# Copyright 2026
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

python early in active_images:
    from typing import Any, Callable, Iterator
    from renpy.ast import Say, Show
    from renpy.display.behavior import Button, render_focus_mask
    from renpy.display.core import Displayable, IgnoreEvent
    from renpy.display.focus import Focus
    from renpy.display.image import ImageReference
    from renpy.display.layout import Null
    from renpy.display.motion import ATLTransform, Transform
    from renpy.display.render import Render
    from renpy.display.scenelists import SceneListEntry, SceneLists
    from renpy.game import CallException, JumpException
    from renpy.lexer import Lexer
    from renpy.rollback import NoRollback
    from renpy.revertable import RevertableDict as python_dict
    from renpy.revertable import RevertableList as python_list

    BUTTON_CLAUSES: frozenset[str] = frozenset({
        "action", "alternate", "hovered", "unhovered", "selected",
        "sensitive", "keysym", "alternate_keysym", "focus_mask",
    })
    SHOW_CLAUSES: frozenset[str] = frozenset({"at", "as", "onlayer", "zorder", "behind", "with"})

    # --- ActiveImage Displayable ---------------------------------------------

    class ActiveImage(Button):
        """Interactive scene-list sprite wrapper inheriting from ``Button``.

        Sits between outer scene transforms (``flip``, ``slot(2)``) and inner
        button transforms (``highlight``, ``on hover:``/``on idle:``).
        """

        def __init__(
            self, child: Displayable | None = None, focus_name: str | None = None, **btn_kwargs: Any
        ) -> None:
            self.focus_name = focus_name
            self.btn_kwargs = dict(btn_kwargs)
            kw = {k: btn_kwargs.get(k) for k in BUTTON_CLAUSES}
            kw["action"] = wrap_click(kw["action"])
            kw["alternate"] = wrap_click(kw["alternate"])
            kw["focus_mask"] = btn_kwargs.get("focus_mask", True)
            super().__init__(child, style="default", focus=focus_name, **kw)

        def visit(self) -> list[Displayable | None]:
            return self.children

        def get_placement(self) -> tuple:
            return self.child.get_placement() if self.child is not None else super().get_placement()

        def set_style_prefix(self, prefix: str, root: bool) -> None:
            if root:
                super().set_style_prefix(prefix, root)
                renpy.display.displayable.default_style.set_prefix("")

        def set_transform_event(self, event: str) -> None:
            super().set_transform_event(event)
            if event in ("show", "replace", "replaced") and self.child is not None:
                if has_atl_event(self.child, {event}):
                    self.child.set_transform_event(event)

        def focus(self, default: bool = False) -> Any:
            rv = super().focus(default)
            redraw_chain(self)
            return rv

        def unfocus(self, default: bool = False) -> None:
            keep_ev = (
                default and self.is_sensitive()
                and getattr(self.child, "transform_event", None) in ("show", "replace")
                and has_atl_event(self.child, {self.child.transform_event})
            )
            (super(Button, self) if keep_ev else super()).unfocus(default)
            redraw_chain(self)

        def render(self, width: int, height: int, st: float, at: float) -> Render:
            surf = renpy.display.render.render(self.child, width, height, st, at)
            sw, sh = surf.get_size()
            rv = Render(sw, sh)
            rv.blit(surf, (0, 0))
            self.offsets = [(0, 0)]
            if self.is_sensitive():
                fx, fy, fw, fh = compute_active_focus_rect(surf, sw, sh)
                mask = render_focus_mask(self.style.focus_mask, sw, sh, st, at, surf)
                if mask is not None:
                    mask = make_occluded_focus_mask(rv, mask)
                rv.add_focus(self, None, fx, fy, fw, fh, None, None, mask)
            return rv

    # --- Transform Hierarchy & SceneLists Integration ------------------------

    class _PendingState:
        op: str | None = None
        btn_kwargs: dict[str, Any] = {}
        what: Displayable | None = None

    def iter_chain(d: Displayable | None) -> Iterator[Displayable]:
        while d is not None:
            yield d
            d = getattr(d, "child", None)

    def get_sle(layer: str, key: str) -> SceneListEntry | None:
        layers = renpy.game.context().scene_lists.layers.get(layer, ())
        return next((sle for sle in reversed(layers) if sle.tag == key), None)

    def find_active_image(d: Displayable | None) -> ActiveImage | None:
        return next((n for n in iter_chain(d) if isinstance(n, ActiveImage)), None)

    def redraw_chain(d: Displayable | None) -> None:
        for n in iter_chain(d):
            renpy.display.render.redraw(n, 0)

    def transform_handles_event(t: Transform, events: set[str]) -> bool:
        if isinstance(t, ATLTransform):
            block = t.get_block() or t.compile()
            return block is not None and any(block._handles_event(ev) for ev in events)
        return False

    def has_atl_event(d: Displayable | None, events: set[str]) -> bool:
        return any(isinstance(n, Transform) and transform_handles_event(n, events) for n in iter_chain(d))

    def is_button_transform(t: Transform) -> bool:
        if t.arguments and any(k for k in t.arguments if k != ""):
            return True
        return transform_handles_event(t, set(renpy.config.containers_pass_transform_events))

    def init_transform_chain(d: Displayable | None) -> None:
        if d is not None:
            for t in reversed([n for n in iter_chain(d) if isinstance(n, Transform)]):
                if not t.active:
                    t.update_state()
            d.get_placement()

    def split_transform_chain(d: Displayable) -> tuple[list[Transform], list[Transform], Displayable]:
        scene_chain: list[Transform] = []
        button_chain: list[Transform] = []
        curr: Displayable = d
        while isinstance(curr, Transform) and curr.child is not None:
            (button_chain if is_button_transform(curr) else scene_chain).append(curr)
            curr = curr.child
        scene_chain.reverse()
        button_chain.reverse()
        return scene_chain, button_chain, curr

    def attach_transform_child(node: Transform, child: Displayable) -> Transform:
        if node.style is renpy.display.displayable.default_style:
            node.style_arg = "transform"
            node.style = renpy.style.Style("transform")
        node.set_child(child, duplicate=False)
        if hasattr(node, "raw_child"):
            node.raw_child = child
        node._unique()
        return node

    def chain_transforms(transforms: list[Transform], leaf: Displayable, copy: bool = False) -> Displayable:
        curr: Displayable = leaf
        for t in transforms:
            if copy:
                t.get_placement()
            curr = attach_transform_child(t.copy() if copy else t, curr)
        return curr

    def unwrap_active_image(d: Displayable) -> Displayable:
        if isinstance(d, ActiveImage):
            return unwrap_active_image(d.child) if d.child is not None else Null()
        if isinstance(d, Transform) and d.child is not None:
            d.get_placement()
            rv = d.copy()
            rv.active = d.active
            return attach_transform_child(rv, unwrap_active_image(d.child))
        return d

    def transfer_active_state(old_widget: ActiveImage, new_widget: ActiveImage) -> None:
        new_widget.role = old_widget.role
        replaced_by = renpy.display.focus.replaced_by
        for k, v in list(replaced_by.items()):
            if v is old_widget:
                replaced_by[k] = new_widget
        replaced_by[id(old_widget)] = new_widget
        if "hover_" in (old_widget.style.prefix or ""):
            new_widget.focus(default=True)
        else:
            new_widget.unfocus(default=True)

    orig_scenelists_add = getattr(SceneLists.add, "orig_scenelists_add", SceneLists.add)

    def add_active_scene_entry(
        self: SceneLists, layer: str, thing: Displayable, key: str | None = None,
        zorder: int | None = 0, behind: list[str] = [], at_list: list[Any] = [],
        name: tuple[str, ...] | None = None, atl: Any = None,
        default_transform: Any = None, transient: bool = False, keep_st: bool = False,
    ) -> None:
        op, _PendingState.op = _PendingState.op, None
        btn_kwargs, _PendingState.btn_kwargs = _PendingState.btn_kwargs, {}

        eff_key = key or (name[0] if name else None)
        old_sle = get_sle(layer, eff_key) if eff_key else None
        old_active = find_active_image(old_sle.displayable) if old_sle is not None else None

        if op is None and old_active is None:
            return orig_scenelists_add(
                self, layer, thing, key, zorder, behind, at_list,
                name, atl, default_transform, transient, keep_st,
            )

        old_btn: list[Transform] = []
        if old_sle is not None:
            init_transform_chain(old_sle.displayable)
            if old_active is not None:
                init_transform_chain(old_active.child)
                _, old_btn, _ = split_transform_chain(old_active.child)
            else:
                old_scene, old_btn, old_leaf = split_transform_chain(old_sle.displayable)
                if old_btn:
                    sle_copy = old_sle.copy()
                    sle_copy.displayable = chain_transforms(old_scene, old_leaf, copy=True)
                    layer_list = self.layers[layer]
                    layer_list[layer_list.index(old_sle)] = sle_copy

        if atl is not None:
            thing = ATLTransform(atl, child=thing)

        new_scene, new_btn, leaf = split_transform_chain(thing)
        old_inner = chain_transforms(old_btn, leaf, copy=True) if old_btn else None
        new_inner = chain_transforms(new_btn, leaf) if new_btn else None
        inner_d = (
            self.transform_state(old_inner, new_inner, execution=True)
            if (old_inner and new_inner)
            else (new_inner or old_inner or leaf)
        )

        merged_kwargs = (
            {**old_active.btn_kwargs, **btn_kwargs}
            if old_active is not None
            else {"action": renpy.store.NullAction(), "focus_mask": True, **btn_kwargs}
        )
        active_img = ActiveImage(inner_d, focus_name=f"active_image:{layer}:{eff_key}", **merged_kwargs)
        outer_d = chain_transforms(new_scene, active_img) if new_scene else active_img

        orig_scenelists_add(
            self, layer, outer_d, key, zorder, behind, new_scene,
            name, None, default_transform, transient, keep_st,
        )

        if op == "deactivate":
            was_focused = old_active is not None and renpy.display.focus.get_focused() is old_active
            active_img.unfocus(default=not was_focused)
            if eff_key and (new_sle := get_sle(layer, eff_key)) is not None:
                new_sle.displayable = unwrap_active_image(new_sle.displayable)
        elif old_active is not None:
            transfer_active_state(old_active, active_img)
        else:
            active_img.unfocus(default=True)

    add_active_scene_entry.orig_scenelists_add = orig_scenelists_add
    SceneLists.add = add_active_scene_entry

    # --- Custom Statements: activate, deactivate, and call active ------------

    def parse_csv(l: Lexer, parser: Callable[[], str | None]) -> list[str]:
        rv: list[str] = []
        while expr := parser():
            rv.append(expr)
            if not l.match(","):
                break
        return rv

    def parse_activate_or_deactivate(l: Lexer, allow_button_clauses: bool = True) -> dict[str, Any]:
        stmt = "activate" if allow_button_clauses else "deactivate"
        name, expression, tag, at_list, layer, zorder, behind = renpy.parser.parse_image_specifier(l)
        at_list, behind = list(at_list), list(behind or ())
        seen_show = {
            k for k, v in (("at", at_list), ("as", tag), ("onlayer", layer), ("zorder", zorder), ("behind", behind)) if v
        }
        button_clauses: dict[str, str] = {}
        with_expr: str | None = None

        while not l.eol() and not l.match(":"):
            pos = l.checkpoint()
            if not (word := l.word()):
                l.error(f"expected clause in {stmt} statement.")
            if word in SHOW_CLAUSES:
                if word in seen_show:
                    l.error(f"duplicate '{word}' clause in {stmt} statement.")
                seen_show.add(word)
                if word == "at":
                    at_list.extend(parse_csv(l, l.simple_expression))
                elif word == "behind":
                    behind.extend(parse_csv(l, l.name))
                elif word in ("as", "onlayer"):
                    tag, layer = (l.require(l.name), layer) if word == "as" else (tag, l.require(l.name))
                else:
                    val = l.require(l.simple_expression)
                    with_expr, zorder = (val, zorder) if word == "with" else (with_expr, val)
            elif allow_button_clauses and word in BUTTON_CLAUSES:
                if word in button_clauses:
                    l.error(f"duplicate '{word}' clause in {stmt} statement.")
                button_clauses[word] = l.require(l.simple_expression)
            else:
                l.revert(pos)
                l.error(f"unexpected clause '{word}' in {stmt} statement.")

        imspec = (name, expression, tag, at_list, layer, zorder, behind)
        return {"imspec": imspec, "button_clauses": button_clauses, "with_expr": with_expr}

    def resolve_imspec(imspec: tuple) -> tuple[tuple, str | None, str]:
        name, expression, tag, at_list, layer, zorder, behind = imspec
        key = tag or (name[0] if name else None)
        resolved_layer = renpy.exports.default_layer(layer, key, bool(expression and not tag))
        if expression is None and key is not None and (sle := get_sle(resolved_layer, key)) is not None:
            d = sle.displayable
            while isinstance(d, (Transform, ActiveImage)) and d.child is not None:
                d = d.child
            if isinstance(d, ImageReference) and isinstance(d.name, tuple) and d.name:
                if (
                    name[0] == key != d.name[0]
                    and renpy.display.image.images.get(name) is None
                    and not renpy.display.image.image_attributes.get(key)
                ):
                    imspec = ((d.name[0],) + name[1:], None, key, at_list, layer, zorder, behind)
            elif d is not None and not isinstance(d, ImageReference) and name == (key,):
                _PendingState.what = d
                expr = "__import__('store').active_images._PendingState.what"
                imspec = (name, expr, key, at_list, layer, zorder, behind)
        return imspec, key, resolved_layer

    def execute_activate_or_deactivate(p: dict[str, Any], atl: Any = None, op: str = "activate") -> None:
        imspec, key, resolved_layer = resolve_imspec(p["imspec"])
        if op == "deactivate" and not renpy.exports.showing(key, layer=resolved_layer):
            return
        if (with_expr := p["with_expr"]) is not None:
            renpy.exports.with_statement(None)

        _PendingState.op = op
        _PendingState.btn_kwargs = {kw: renpy.python.py_eval(expr) for kw, expr in p["button_clauses"].items()}
        try:
            renpy.ast.show_imspec(imspec, atl=atl)
        finally:
            _PendingState.op, _PendingState.btn_kwargs, _PendingState.what = None, {}, None

        if with_expr is not None:
            renpy.exports.with_statement(renpy.python.py_eval(with_expr))

    def lint_activate_or_deactivate(p: dict[str, Any], is_deactivate: bool = False) -> None:
        imspec = p["imspec"]
        name, expression, tag, at_list, layer, zorder, behind = imspec
        stmt = "deactivate" if is_deactivate else "activate"
        node = Show((None, 0), imspec)

        if is_deactivate and expression is None:
            renpy.lint.check_hide(node)
            if zorder is not None:
                renpy.lint.try_eval(f"the zorder of a {stmt} statement", zorder)
            for expr in at_list:
                renpy.lint.try_eval(
                    f"the at list of a {stmt} statement", expr,
                    "Perhaps you forgot to define or misspelled a transform.",
                )
        else:
            renpy.lint.check_show(node, False)

        for kw, expr in p.get("button_clauses", {}).items():
            renpy.lint.try_eval(f"the {kw} clause of a {stmt} statement", expr)
        if p.get("with_expr") is not None:
            renpy.lint.try_eval(f"the with clause of a {stmt} statement", p["with_expr"])

    def parse_call_active(l: Lexer) -> dict[str, Any]:
        delay = l.simple_expression()
        subparse = None
        if l.match(":"):
            l.expect_block("call active statement")
            subparse = l.subblock_lexer().renpy_block()
            if len(subparse.block) != 1 or not isinstance(subparse.block[0], Say):
                l.error("call active block may only contain a single say statement.")
            subparse.block[0].interact = False
        else:
            l.expect_noblock("call active statement")
        l.expect_eol()
        return {"delay": delay, "subparse": subparse}

    def execute_call_active(p: dict[str, Any]) -> None:
        renpy.exports.choice_for_skipping()
        renpy.config.skipping = None
        renpy.store._begin_rollback = False
        if p["subparse"] is None:
            renpy.store._window_auto = True

    def post_execute_call_active(p: dict[str, Any]) -> None:
        renpy.store._begin_rollback = True
        delay = renpy.python.py_eval(p["delay"]) if p["delay"] is not None else None
        roll_forward = renpy.exports.roll_forward_info()

        if (
            delay is not None
            and renpy.game.after_rollback
            and (not renpy.config.pause_after_rollback or roll_forward is None)
        ):
            delay = 0

        renpy.exports.mode("pause")
        RollForwardState.in_call_active = True
        try:
            rv = renpy.ui.interact(mouse="pause", type="pause", roll_forward=roll_forward, pause=delay)
        except (JumpException, CallException) as e:
            rv = e
        finally:
            RollForwardState.in_call_active = False

        renpy.exports.checkpoint(
            rv, keep_rollback=True, hard=renpy.config.pause_after_rollback or (delay is None)
        )
        if renpy.config.implicit_with_none:
            renpy.game.interface.do_with(None, None)
        if isinstance(rv, (JumpException, CallException)):
            raise rv
        renpy.store._return = rv

    def lint_call_active(p: dict[str, Any]) -> None:
        if p["delay"] is not None:
            renpy.lint.try_eval("the delay of a call active statement", p["delay"])

    for _stmt, _allow_btn in (("activate", True), ("deactivate", False)):
        renpy.register_statement(
            _stmt,
            parse=lambda l, b=_allow_btn: parse_activate_or_deactivate(l, allow_button_clauses=b),
            execute=lambda p, atl=None, op=_stmt: execute_activate_or_deactivate(p, atl, op=op),
            predict=lambda p: (renpy.ast.predict_imspec(p["imspec"]), [])[1],
            lint=lambda p, d=not _allow_btn: lint_activate_or_deactivate(p, is_deactivate=d),
            block="atl-possible",
            warp=lambda p: True,
        )
    renpy.register_statement(
        "call active",
        parse=parse_call_active,
        execute=execute_call_active,
        post_execute=post_execute_call_active,
        next=lambda p: p["subparse"],
        lint=lint_call_active,
        block="possible",
        force_begin_rollback=True,
    )

    # --- Spatial Focus Bounds, Occlusion, and Directional Navigation ---------

    FOCUS_DIRECTIONS: dict[str, tuple[int, int]] = {
        "focus_right": (0, 1), "focus_left": (0, -1), "focus_down": (1, 1), "focus_up": (1, -1),
    }

    def get_render_content_bounds(r: Render) -> tuple[float, float, float, float] | None:
        bx0 = by0 = float("inf")
        bx1 = by1 = float("-inf")
        rev = getattr(r, "reverse", None)
        has_matrix = rev is not None and (rev.xdx, rev.xdy, rev.ydx, rev.ydy) != (1.0, 0.0, 0.0, 1.0)
        for child, xo, yo, focus, main in r.children:
            if isinstance(child, Render):
                cb = get_render_content_bounds(child)
            elif hasattr(child, "get_bounding_rect"):
                rx, ry, rw, rh = child.get_bounding_rect()
                cb = (float(rx), float(ry), float(rx + rw), float(ry + rh)) if (rw > 0 and rh > 0) else None
            elif hasattr(child, "get_size"):
                cw, ch = child.get_size()
                cb = (0.0, 0.0, float(cw), float(ch)) if (cw > 0 and ch > 0) else None
            else:
                cb = None
            if cb is None:
                continue
            cx0, cy0, cx1, cy1 = cb
            if has_matrix:
                xs, ys = zip(*(rev.transform(x, y) for x in (cx0, cx1) for y in (cy0, cy1)))
                tx0, ty0, tx1, ty1 = min(xs) + xo, min(ys) + yo, max(xs) + xo, max(ys) + yo
            else:
                tx0, ty0, tx1, ty1 = cx0 + xo, cy0 + yo, cx1 + xo, cy1 + yo
            bx0, by0, bx1, by1 = min(bx0, tx0), min(by0, ty0), max(bx1, tx1), max(by1, ty1)

        if getattr(r, "xclipping", False):
            bx0, bx1 = max(0.0, bx0), min(float(r.width), bx1)
        if getattr(r, "yclipping", False):
            by0, by1 = max(0.0, by0), min(float(r.height), by1)
        return (bx0, by0, bx1, by1) if (bx0 < bx1 and by0 < by1) else None

    def compute_active_focus_rect(surf: Render, sw: float, sh: float) -> tuple[float, float, float, float]:
        if (bounds := get_render_content_bounds(surf)) is not None:
            bx0, by0 = max(0.0, bounds[0]), max(0.0, bounds[1])
            bx1, by1 = min(float(sw), bounds[2]), min(float(sh), bounds[3])
        else:
            bx0, by0, bx1, by1 = 0.0, 0.0, float(sw), float(sh)
        return (bx0, by0, max(1.0, bx1 - bx0), max(1.0, by1 - by0))

    def is_pixel_occluded(rv: Render, cx: float, cy: float) -> bool:
        curr = rv
        while found := next(
            ((p, i, xo, yo) for p in curr.parents for i, (c, xo, yo, f, _) in enumerate(p.children) if c is curr and f),
            None,
        ):
            parent, idx, xo, yo = found
            if parent.reverse is not None:
                cx, cy = parent.reverse.transform(cx, cy)
            px, py = cx + xo, cy + yo
            if (parent.xclipping and not (0 <= px < parent.width)) or (
                parent.yclipping and not (0 <= py < parent.height)
            ):
                return True
            for sib, sxo, syo, _, _ in parent.children[idx + 1 :]:
                if isinstance(sib, Render):
                    sx, sy = px - sxo, py - syo
                    if parent.forward is not None:
                        sx, sy = parent.forward.transform(sx, sy)
                    if sib.is_pixel_opaque(round(sx), round(sy)):
                        return True
            curr, cx, cy = parent, px, py
        return False

    def make_occluded_focus_mask(rv: Render, base_mask: Any) -> Callable[[int | float, int | float], bool]:
        def focus_mask(mx: int | float, my: int | float) -> bool:
            if isinstance(base_mask, Render):
                if not base_mask.is_pixel_opaque(int(mx), int(my)):
                    return False
            elif callable(base_mask) and not base_mask(mx, my):
                return False
            return not is_pixel_occluded(rv, mx, my)

        return focus_mask

    def get_focus_spans(
        f: Focus, collapse: tuple[bool, bool] = (False, False)
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        x, y, w, h = f.inset_rect()
        if not isinstance(f.widget, ActiveImage):
            return ((float(x), float(x + w)), (float(y), float(y + h)))
        limits = (renpy.config.screen_width, renpy.config.screen_height)
        spans = []
        for orig, size, lim, col in zip((x, y), (w, h), limits, collapse):
            p0, p1 = float(orig), float(orig + size)
            if (c0 := max(0.0, p0)) < (c1 := min(float(lim), p1)):
                p0, p1 = c0, c1
            spans.append(((p0 + p1) * 0.5, (p0 + p1) * 0.5) if col else (p0, p1))
        return (spans[0], spans[1])

    def find_directional_focus(from_focus: Focus, candidates: list[Focus], spec: tuple[int, int]) -> Focus | None:
        axis, step = spec
        perp = 1 - axis
        from_active = isinstance(from_focus.widget, ActiveImage)
        penalty = renpy.config.focus_crossrange_penalty
        best, best_score = None, (float("inf"), float("inf"))
        for f in candidates:
            if f.widget is from_focus.widget and f.arg == from_focus.arg:
                continue
            to_active = isinstance(f.widget, ActiveImage)
            cross = from_active != to_active
            collapse = (axis == 0 or cross, axis == 1 or cross)
            o_spans, n_spans = get_focus_spans(from_focus, collapse), get_focus_spans(f, collapse)
            (om0, om1), (op0, op1) = o_spans[axis], o_spans[perp]
            (nm0, nm1), (np0, np1) = n_spans[axis], n_spans[perp]
            omc, opc = (om0 + om1) * 0.5, (op0 + op1) * 0.5
            nmc, npc = (nm0 + nm1) * 0.5, (np0 + np1) * 0.5
            if cross and om0 < nm1 and nm0 < om1:
                om0 = om1 = omc
                nm0 = nm1 = nmc
            if (nmc - omc) * step <= 0 or (step > 0 and om1 > nm0) or (step < 0 and nm1 > om0):
                continue
            main_gap = (nm0 - om1) if step > 0 else (om0 - nm1)
            perp_gap = max(0.0, np0 - op1, op0 - np1)
            perp_diff = abs(npc - opc)
            if (cross and perp_gap > main_gap) or (from_active and to_active and perp_diff > main_gap):
                continue
            score = (main_gap**2 + (perp_gap * penalty) ** 2, perp_diff)
            if score < best_score:
                best, best_score = f, score
        return best

    def handle_active_spatial_focus(ev: Any) -> tuple[bool, Any]:
        if renpy.game.preferences.self_voicing:
            return False, None
        direction = next((d for d in FOCUS_DIRECTIONS if renpy.display.behavior.map_event(ev, d)), None)
        if direction is None:
            return False, None

        spatial_focuses = [
            f for f in renpy.display.focus.focus_list
            if f.widget.style.keyboard_focus
            and all(v is not None and v is not False for v in (f.x, f.y, f.w, f.h))
        ]
        active_focuses = [f for f in spatial_focuses if isinstance(f.widget, ActiveImage)]
        if not active_focuses:
            return False, None

        renpy.display.focus.check_keyboard_focus()
        renpy.display.focus.pending_focus_type = "keyboard"

        current = renpy.display.focus.get_focused()
        from_focus = next(
            (f for f in spatial_focuses if current is not None and f.widget is current and f.arg == renpy.display.focus.argument),
            None,
        )
        spec = FOCUS_DIRECTIONS[direction]
        axis, step = spec
        perp = 1 - axis
        if from_focus is None:
            new_focus = min(
                active_focuses,
                key=lambda f: (-step * (s := get_focus_spans(f, (True, True)))[axis][0], s[perp][0]),
            )
        else:
            new_focus = find_directional_focus(from_focus, spatial_focuses, spec)
        return True, (renpy.display.focus.change_focus(new_focus) if new_focus is not None else None)

    # --- Rollback and Rollforward State --------------------------------------

    class RollForwardState(NoRollback):
        forward_clicks: dict[Any, list[Any]] = python_dict()
        active_rb_id: Any = None
        active_node_name: Any = None
        interacted_in_node: bool = False
        current_node_clicks: list[Any] = python_list()
        in_call_active: bool = False

    class ActiveRollForward(Null):
        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            handled, focus_rv = handle_active_spatial_focus(ev)
            if handled:
                if focus_rv is not None:
                    return focus_rv
                raise IgnoreEvent()

            rf_target = getattr(renpy.game.interface, "roll_forward", None)
            if rf_target is None or not renpy.display.behavior.map_event(ev, "rollforward"):
                return None

            _, node_name = get_current_rollback_info()
            if not (actions := RollForwardState.forward_clicks.get(node_name) if node_name is not None else None):
                return None

            RollForwardState.interacted_in_node = True
            rf_val = renpy.exports.roll_forward_core(rf_target)
            try:
                for act in actions:
                    rv = renpy.display.behavior.run(act)
                    if rv is not None and not isinstance(rv, (JumpException, CallException)):
                        renpy.store._return = rv
            except (JumpException, CallException) as e:
                if not isinstance(rf_val, (JumpException, CallException)):
                    return e
            return rf_val

    def get_current_rollback_info() -> tuple[Any, Any]:
        log = getattr(renpy.game, "log", None)
        return (log.current.identifier, log.current.context.current) if (log and log.current) else (None, None)

    def on_start_interact() -> None:
        if renpy.game.interface.trans_pause or not renpy.game.context().rollback:
            return
        rb_id, node_name = get_current_rollback_info()
        if rb_id is None or rb_id == RollForwardState.active_rb_id:
            return

        if (prev_node := RollForwardState.active_node_name) is not None and not RollForwardState.interacted_in_node:
            if all(f.name != prev_node for f in getattr(renpy.game.log, "forward", ())):
                RollForwardState.forward_clicks.pop(prev_node, None)

        RollForwardState.active_rb_id, RollForwardState.active_node_name = rb_id, node_name
        RollForwardState.interacted_in_node = False
        RollForwardState.current_node_clicks = python_list()

    def wrap_click(action: Any) -> Callable[[], Any] | None:
        if action is None:
            return None

        def wrapped() -> Any:
            _, node_name = get_current_rollback_info()
            if node_name is not None and renpy.game.context().rollback:
                RollForwardState.interacted_in_node = True
                RollForwardState.current_node_clicks.append(action)
                RollForwardState.forward_clicks[node_name] = python_list(RollForwardState.current_node_clicks)
            try:
                rv = renpy.display.behavior.run(action)
            except (JumpException, CallException):
                renpy.display.focus.old_max_default = 0
                raise
            if rv is not None:
                renpy.display.focus.old_max_default = 0
                if not isinstance(rv, (JumpException, CallException)):
                    renpy.store._return = rv
                    if (
                        not RollForwardState.in_call_active
                        and renpy.exports.current_interact_type() in ("say", "pause")
                        and type(rv) is not bool
                    ):
                        return True
            return rv

        return wrapped

define config.underlay += [active_images.ActiveRollForward()]
define config.start_interact_callbacks += [active_images.on_start_interact]
