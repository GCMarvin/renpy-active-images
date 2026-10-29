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
    from renpy.display.scenelists import SceneListEntry
    from renpy.exports.displayexports import _find_image
    from renpy.game import CallException, JumpException
    from renpy.lexer import Lexer
    from renpy.parser import parse_image_specifier, parse_simple_expression_list
    from renpy.rollback import NoRollback
    from renpy.revertable import RevertableDict as python_dict
    from renpy.revertable import RevertableList as python_list

    BUTTON_CLAUSES: frozenset[str] = frozenset({
        "action", "alternate", "hovered", "unhovered", "selected",
        "sensitive", "keysym", "alternate_keysym", "focus_mask",
    })
    SHOW_CLAUSES: frozenset[str] = frozenset({"at", "as", "onlayer", "zorder", "behind", "with"})
    FOCUS_DIRECTIONS: dict[str, tuple[int, int]] = {
        "focus_right": (0, 1), "focus_left": (0, -1), "focus_down": (1, 1), "focus_up": (1, -1),
    }

    _pending: tuple[str, dict[str, Any]] | None = None

    # --- ActiveImage Displayable ---------------------------------------------

    class ActiveImage(Button):
        """Interactive scene-list sprite wrapper inheriting from Ren'Py's Button.

        Positioned in the scene-list displayable chain between outer scene
        transforms (such as placement or flipping) and inner button transforms
        (such as hover/idle ATL blocks or style-prefixed properties). This split
        lets outer transforms position the sprite on screen while ActiveImage
        intercepts mouse/keyboard events and forwards hover/idle state only to
        the inner button transforms.
        """

        def __init__(self, child: Displayable | None = None, focus_name: str | None = None, **btn_kwargs: Any) -> None:
            """Initialize the active image button wrapper.

            Wraps action callbacks for rollback/roll-forward tracking and
            defaults focus_mask to True so transparent sprite padding ignores
            pointer events.

            Args:
                child: Inner displayable chain (button transforms and leaf image).
                focus_name: Unique focus identifier for SceneLists replacement tracking.
                **btn_kwargs: Keyword arguments matching standard Button clauses.
            """
            self.focus_name = focus_name
            self.btn_kwargs = dict(btn_kwargs)
            kw = {k: btn_kwargs.get(k) for k in BUTTON_CLAUSES} | {
                "action": wrap_click(btn_kwargs.get("action")),
                "alternate": wrap_click(btn_kwargs.get("alternate")),
                "focus_mask": btn_kwargs.get("focus_mask", True),
            }
            super().__init__(child, style="default", focus=focus_name, **kw)

        def _change_transform_child(self, child: Displayable) -> Displayable:
            """Clone this ActiveImage around a new leaf child during SceneLists updates.

            Displayable._change_transform_child normally returns child directly,
            which would discard a Button wrapper when Ren'Py's SceneLists.add
            updates an image without new transforms. Overriding this method
            preserves both the ActiveImage wrapper and any inner button transforms.

            Args:
                child: New replacement displayable passed by SceneLists.add.

            Returns:
                Displayable: Either child if already wrapped in ActiveImage, or a
                new ActiveImage preserving inner button transforms and focus state.
            """
            if isinstance(child, ActiveImage):
                return child
            init_transform_chain(self.child)
            _, old_btn, _ = split_transform_chain(self.child) if self.child is not None else ([], [], None)
            inner = chain_transforms(old_btn, child, copy=True) if old_btn else child
            rv = ActiveImage(inner, focus_name=self.focus_name, **self.btn_kwargs)
            transfer_active_state(self, rv)
            return rv

        def get_placement(self) -> tuple:
            """Delegate placement queries to the wrapped child displayable.

            Button inherits Window placement defaults, which would override an
            ImageReference's image_placement style unless delegated to child.

            Returns:
                tuple: Placement tuple (xpos, ypos, xanchor, yanchor, xoffset,
                yoffset, subpixel) from the child displayable.
            """
            return self.child.get_placement() if self.child is not None else super().get_placement()

        def set_style_prefix(self, prefix: str, root: bool) -> None:
            """Propagate button style prefixes only when triggered by ActiveImage itself.

            Outer scene transforms call set_style_prefix with root=False; ignoring
            those prevents outer transforms from overwriting the button's own
            hover_ or idle_ prefix. Also resets default_style's prefix so global
            displayables sharing default_style are not polluted.

            Args:
                prefix: Style prefix string (such as 'hover_' or 'idle_').
                root: True when initiated directly by this button's focus state.
            """
            if root:
                super().set_style_prefix(prefix, root)
                renpy.display.displayable.default_style.set_prefix("")

        def set_transform_event(self, event: str) -> None:
            """Forward scene-list lifecycle transform events to inner transforms selectively.

            Container does not forward 'show', 'replace', or 'replaced' to its
            children, whereas unconditional forwarding would overwrite 'hover' or
            'idle' on inner button transforms that do not handle scene events.
            This override forwards scene lifecycle events only when an inner
            transform explicitly defines an ATL handler for them.

            Args:
                event: Transform event name (such as 'show', 'replace', 'hover', 'idle').
            """
            super().set_transform_event(event)
            if event in ("show", "replace", "replaced") and self.child is not None:
                if has_atl_event(self.child, {event}):
                    self.child.set_transform_event(event)

        def focus(self, default: bool = False) -> Any:
            """Handle focus gain and schedule a redraw across the inner transform chain.

            Args:
                default: True when restoring initial focus without firing hovered.

            Returns:
                Any: Result of the hovered action if executed, otherwise None.
            """
            rv = super().focus(default)
            self.visit_all(lambda d: renpy.display.render.redraw(d, 0))
            return rv

        def unfocus(self, default: bool = False) -> None:
            """Handle focus loss while preserving initial 'show'/'replace' ATL events.

            Button.unfocus unconditionally sets the 'idle' transform event on its
            child, which would abort an inner transform's 'on show:' or
            'on replace:' animation during initial scene setup (default=True).
            When such an event is pending, this calls Window.unfocus directly.

            Args:
                default: True when setting initial unfocused state without firing unhovered.
            """
            keep_ev = (
                default and self.is_sensitive()
                and getattr(self.child, "transform_event", None) in ("show", "replace")
                and has_atl_event(self.child, {self.child.transform_event})
            )
            (super(Button, self) if keep_ev else super()).unfocus(default)
            self.visit_all(lambda d: renpy.display.render.redraw(d, 0))

        def render(self, width: int, height: int, st: float, at: float) -> Render:
            """Render the child sprite directly and register an occlusion-aware focus mask.

            Button.render uses Window layout (adding window padding, background,
            and sizing), which is unsuitable for scene-list sprites. This method
            renders the child directly like an image while registering a focus
            rectangle tightened to the visible sprite bounds and wrapped with
            scene-layer occlusion checking.

            Args:
                width: Available horizontal space in pixels.
                height: Available vertical space in pixels.
                st: Shown time in seconds.
                at: Animation time in seconds.

            Returns:
                Render: Render surface containing the child sprite and focus region.
            """
            surf = renpy.display.render.render(self.child, width, height, st, at)
            sw, sh = surf.get_size()
            rv = Render(sw, sh)
            rv.blit(surf, (0, 0))
            self.offsets = [(0, 0)]
            if self.is_sensitive():
                b = get_render_content_bounds(surf)
                bx0, by0, bx1, by1 = (max(0.0, b[0]), max(0.0, b[1]), min(float(sw), b[2]), min(float(sh), b[3])) if b else (0.0, 0.0, float(sw), float(sh))
                mask = render_focus_mask(self.style.focus_mask, sw, sh, st, at, surf)
                if mask is not None:
                    mask = make_occluded_focus_mask(rv, mask)
                rv.add_focus(self, None, bx0, by0, max(1.0, bx1 - bx0), max(1.0, by1 - by0), None, None, mask)
            return rv

    # --- Transform Hierarchy & config.show Integration -----------------------

    def iter_chain(d: Displayable | None) -> Iterator[Displayable]:
        """Yield each displayable along a single-child wrapper chain.

        Args:
            d: Root displayable of the chain.

        Yields:
            Displayable: Each displayable from root to leaf.
        """
        while d is not None:
            yield d
            d = getattr(d, "child", None)

    def get_sle(layer: str, key: str) -> SceneListEntry | None:
        """Return the latest SceneListEntry for tag key on layer, or None.

        Args:
            layer: Scene layer name.
            key: Image tag to look up.

        Returns:
            SceneListEntry | None: Matching entry on the layer if present.
        """
        layers = renpy.game.context().scene_lists.layers.get(layer, ())
        return next((sle for sle in reversed(layers) if sle.tag == key), None)

    def find_active_image(d: Displayable | None) -> ActiveImage | None:
        """Find the ActiveImage instance inside a displayable wrapper chain.

        Args:
            d: Root displayable to search.

        Returns:
            ActiveImage | None: The wrapped ActiveImage if present, else None.
        """
        return next((n for n in iter_chain(d) if isinstance(n, ActiveImage)), None)

    def transform_handles_event(t: Transform, events: set[str]) -> bool:
        """Check whether an ATLTransform's compiled block handles any of events.

        Args:
            t: Transform to inspect.
            events: Set of transform event names to check for.

        Returns:
            bool: True if t is an ATLTransform with a handler for any given event.
        """
        block = (t.get_block() or t.compile()) if isinstance(t, ATLTransform) else None
        return block is not None and any(block._handles_event(ev) for ev in events)

    def has_atl_event(d: Displayable | None, events: set[str]) -> bool:
        """Check whether any Transform in chain d handles any of events.

        Args:
            d: Root displayable of the transform chain.
            events: Set of transform event names to search for.

        Returns:
            bool: True if any Transform in the chain handles one of events.
        """
        return any(isinstance(n, Transform) and transform_handles_event(n, events) for n in iter_chain(d))

    def is_button_transform(t: Transform) -> bool:
        """Classify whether a Transform belongs inside ActiveImage (button transform).

        A transform is treated as an inner button transform if it responds to
        button container events (such as 'hover' or 'idle' in
        config.containers_pass_transform_events) or receives keyword arguments.
        Otherwise it is treated as an outer scene transform.

        Args:
            t: Transform to classify.

        Returns:
            bool: True if t should be placed inside ActiveImage, False if outside.
        """
        return bool(t.arguments and any(k for k in t.arguments if k != "")) or transform_handles_event(
            t, set(renpy.config.containers_pass_transform_events)
        )

    def init_transform_chain(d: Displayable | None) -> None:
        """Ensure unrendered Transforms in chain d have initialized state before copying.

        When an image is shown and then immediately activated or re-shown before
        an interaction renders it, Transform.update_state has not yet run.
        Initializing bottom-up ensures Transform.copy and SceneLists.transform_state
        preserve valid coordinates and state.

        Args:
            d: Root displayable of the transform chain to initialize.
        """
        if d is not None:
            for t in reversed([n for n in iter_chain(d) if isinstance(n, Transform)]):
                if not t.active:
                    t.update_state()
            d.get_placement()

    def split_transform_chain(d: Displayable) -> tuple[list[Transform], list[Transform], Displayable]:
        """Partition a displayable chain into outer scene transforms, inner button transforms, and leaf.

        Args:
            d: Displayable wrapped in zero or more Transforms.

        Returns:
            tuple[list[Transform], list[Transform], Displayable]: A 3-tuple of
            (scene_transforms, button_transforms, leaf_displayable) ordered from
            innermost to outermost so chain_transforms can rebuild them bottom-up.
        """
        scene_chain: list[Transform] = []
        button_chain: list[Transform] = []
        curr: Displayable = d
        while isinstance(curr, Transform) and curr.child is not None:
            (button_chain if is_button_transform(curr) else scene_chain).append(curr)
            curr = curr.child
        return scene_chain[::-1], button_chain[::-1], curr

    def attach_transform_child(node: Transform, child: Displayable) -> Transform:
        """Attach child to Transform node with an isolated transform style.

        Gives the Transform its own Style('transform') instance if it still
        references default_style, allowing Container.set_style_prefix to
        propagate hover_/idle_ style prefixes to inner transforms without
        mutating the shared default_style.

        Args:
            node: Transform to update.
            child: Child displayable to attach.

        Returns:
            Transform: The updated Transform node.
        """
        if node.style is renpy.display.displayable.default_style:
            node.style_arg = "transform"
            node.style = renpy.style.Style("transform")
        node.set_child(child, duplicate=False)
        node._unique()
        return node

    def chain_transforms(transforms: list[Transform], leaf: Displayable, copy: bool = False) -> Displayable:
        """Wrap leaf in a sequence of Transforms from innermost to outermost.

        Args:
            transforms: List of Transforms ordered from innermost to outermost.
            leaf: Innermost displayable to wrap.
            copy: If True, copy each Transform (preserving its state) before attaching.

        Returns:
            Displayable: Root of the constructed transform chain.
        """
        curr: Displayable = leaf
        for t in transforms:
            if copy:
                t.get_placement()
            curr = attach_transform_child(t.copy() if copy else t, curr)
        return curr

    def transfer_active_state(old_widget: ActiveImage, new_widget: ActiveImage) -> None:
        """Transfer focus replacement mapping and hover/idle state to a replacement ActiveImage.

        Registers old_widget -> new_widget in renpy.display.focus.replaced_by so
        Ren'Py's focus system recognizes new_widget as the same logical button
        across interactions, and applies the matching focus/unfocus state.

        Args:
            old_widget: Previous ActiveImage being replaced on the layer.
            new_widget: Newly created ActiveImage replacing it.
        """
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

    def show(
        name: str | tuple[str, ...], at_list: list[Any] = [], layer: str | None = None,
        what: Any = None, zorder: int | None = 0, tag: str | None = None,
        behind: list[str] = [], atl: Any = None, transient: bool = False,
        munge_name: bool = True, _op: str | None = None, _btn_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Show, update, activate, or deactivate a scene-list image via config.show.

        Registered as config.show so all 'show', 'scene', 'activate', and
        'deactivate' statements route through this function without monkey
        patching Ren'Py classes. For normal non-active images, this delegates
        directly to renpy.exports.show. When activating, deactivating, or
        updating an already-active image, it resolves the leaf image, splits
        outer scene transforms from inner button transforms around ActiveImage,
        delegates scene-list insertion to renpy.exports.show, and transfers
        focus/hover state.

        Args:
            name: Image name tuple or space-separated string.
            at_list: List of transforms to apply.
            layer: Target layer name, or None for the default layer.
            what: Explicit Displayable or image name from 'show expression'.
            zorder: Z-order integer, or None to preserve existing zorder.
            tag: Custom image tag from an 'as' clause.
            behind: List of tags this image should be placed behind.
            atl: Raw ATL block attached to the statement, if any.
            transient: True if shown transiently for a single interaction.
            munge_name: True to replace name[0] with tag when recording the entry.
            _op: Optional active operation ('activate' or 'deactivate').
            _btn_kwargs: Optional evaluated Button keyword arguments for 'activate'.
        """
        global _pending
        if _pending is not None:
            _op, _btn_kwargs = _pending
            _pending = None
        btn_kwargs = _btn_kwargs or {}

        name = name if isinstance(name, tuple) else tuple(name.split())
        sls = renpy.exports.scene_lists()
        key = tag or name[0]
        layer = renpy.exports.default_layer(layer, key)
        old_sle = get_sle(layer, key)
        old_active = find_active_image(old_sle.displayable) if old_sle is not None else None

        if _op == "deactivate" and old_sle is None:
            return
        if _op is None and old_active is None:
            return renpy.exports.show(
                name, at_list=at_list, layer=layer, what=what, zorder=zorder,
                tag=tag, behind=behind, atl=atl, transient=transient, munge_name=munge_name,
            )

        if what is None and old_sle is not None:
            d = next((n for n in iter_chain(old_sle.displayable) if not isinstance(n, (Transform, ActiveImage))), None)
            if isinstance(d, ImageReference) and isinstance(d.name, tuple) and d.name:
                if (
                    name[0] == key != d.name[0]
                    and renpy.display.image.images.get(name) is None
                    and not renpy.display.image.image_attributes.get(key)
                ):
                    name, tag = (d.name[0],) + name[1:], key
            elif d is not None and not isinstance(d, ImageReference) and name == (key,):
                what = d

        if renpy.config.sticky_positions and not at_list and key in sls.at_list[layer]:
            at_list = sls.at_list[layer][key]
        if not at_list and (tt := renpy.config.tag_transform.get(key)) is not None:
            at_list = renpy.easy.to_list(tt, copy=True)

        if not isinstance(what, Displayable):
            name, what = _find_image(layer, key, name, what)
            img: Displayable = ImageReference(what, style="image_placement")
        elif renpy.config.wrap_shown_transforms and isinstance(what, Transform):
            img = ImageReference(what, style="image_placement")
        else:
            img = what

        for i in at_list:
            img = i(child=img) if isinstance(i, Transform) else i(img)
            img._unique()
        if atl is not None:
            img = ATLTransform(atl.atl if isinstance(atl, ATLTransform) else atl, child=img)

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
                    sls.layers[layer][sls.layers[layer].index(old_sle)] = sle_copy

        new_scene, new_btn, leaf = split_transform_chain(img)
        old_inner = chain_transforms(old_btn, leaf, copy=True) if old_btn else None
        new_inner = chain_transforms(new_btn, leaf) if new_btn else None
        inner_d = (
            sls.transform_state(old_inner, new_inner, execution=True)
            if (old_inner and new_inner)
            else (new_inner or old_inner or leaf)
        )

        merged_kwargs = (
            {**old_active.btn_kwargs, **btn_kwargs}
            if old_active is not None
            else {"action": renpy.store.NullAction(), "focus_mask": True, **btn_kwargs}
        )
        active_img = ActiveImage(inner_d, focus_name=f"active_image:{layer}:{key}", **merged_kwargs)
        renpy.exports.show(
            name, at_list=new_scene, layer=layer, what=active_img, zorder=zorder,
            tag=tag, behind=behind, atl=None, transient=transient, munge_name=munge_name,
        )

        if _op == "deactivate":
            was_focused = old_active is not None and renpy.display.focus.get_focused() is old_active
            active_img.unfocus(default=not was_focused)
            if (new_sle := get_sle(layer, key)) is not None and active_img.child is not None:
                if new_sle.displayable is active_img:
                    new_sle.displayable = active_img.child
                elif (p := next((n for n in iter_chain(new_sle.displayable) if getattr(n, "child", None) is active_img), None)):
                    attach_transform_child(p, active_img.child)
        elif old_active is not None:
            transfer_active_state(old_active, active_img)
        else:
            active_img.unfocus(default=True)

    # --- Custom Statements: activate, deactivate, and call active ------------

    def parse_activate_or_deactivate(l: Lexer, allow_button_clauses: bool = True) -> dict[str, Any]:
        """Parse an 'activate' or 'deactivate' statement into an imspec and button clauses.

        Uses Ren'Py's native parse_image_specifier for the leading image and
        show clauses, then continues parsing interspersed button clauses (for
        'activate') and trailing show/with clauses in any order.

        Args:
            l: Ren'Py script lexer positioned after the statement keyword.
            allow_button_clauses: True for 'activate', False for 'deactivate'.

        Returns:
            dict[str, Any]: Parsed statement dictionary containing 'imspec',
            'button_clauses', and 'with_expr'.
        """
        stmt = "activate" if allow_button_clauses else "deactivate"
        name, expression, tag, at_list, layer, zorder, behind = parse_image_specifier(l)
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
                    at_list.extend(parse_simple_expression_list(l, image=True))
                elif word == "behind":
                    while True:
                        behind.append(l.require(l.name))
                        if not l.match(","):
                            break
                elif word in ("as", "onlayer"):
                    tag, layer = (l.require(l.name), layer) if word == "as" else (tag, l.require(l.image_name_component))
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

    def execute_activate_or_deactivate(p: dict[str, Any], atl: Any = None, op: str = "activate") -> None:
        """Execute a parsed 'activate' or 'deactivate' statement.

        Evaluates button clause expressions and delegates image specifier
        execution to renpy.ast.show_imspec, which invokes active_images.show
        via config.show. Wraps execution in paired with_statement calls when a
        'with' transition clause is present, matching Ren'Py's parse_with AST.

        Args:
            p: Parsed statement dictionary from parse_activate_or_deactivate.
            atl: Optional compiled ATL block attached to the statement.
            op: Operation type ('activate' or 'deactivate').
        """
        global _pending
        if (with_expr := p["with_expr"]) is not None:
            renpy.exports.with_statement(None)
        _pending = (op, {kw: renpy.python.py_eval(expr) for kw, expr in p["button_clauses"].items()})
        try:
            renpy.ast.show_imspec(p["imspec"], atl=atl)
        finally:
            _pending = None
        if with_expr is not None:
            renpy.exports.with_statement(renpy.python.py_eval(with_expr))

    def lint_activate_or_deactivate(p: dict[str, Any], is_deactivate: bool = False) -> None:
        """Validate image specifiers and clause expressions during 'renpy lint'.

        Delegates image checking to Ren'Py's native check_show (for 'activate')
        or check_hide (for 'deactivate', since deactivating an already-shown
        tag does not require a full attribute specification), then validates
        all Python expressions in button, at, zorder, and with clauses.

        Args:
            p: Parsed statement dictionary from parse_activate_or_deactivate.
            is_deactivate: True when linting a 'deactivate' statement.
        """
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
        """Parse a 'call active' statement and its optional single-Say subblock.

        When a colon subblock is provided, verifies that it contains a single
        Say statement and sets its interact=False so the dialogue line is
        displayed on screen while post_execute_call_active runs the modal
        active-image interaction.

        Args:
            l: Ren'Py script lexer positioned after 'call active'.

        Returns:
            dict[str, Any]: Dictionary with 'delay' expression and 'subparse' block.
        """
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
        """Prepare skipping, rollback, and window state before 'call active' runs.

        Defers rollback checkpointing until post_execute_call_active so that any
        nested non-interactive Say statement and the subsequent active-image
        interaction share a single rollback checkpoint.

        Args:
            p: Parsed statement dictionary from parse_call_active.
        """
        renpy.exports.choice_for_skipping()
        renpy.config.skipping = None
        renpy.store._begin_rollback = False
        if p["subparse"] is None:
            renpy.store._window_auto = True

    def post_execute_call_active(p: dict[str, Any]) -> None:
        """Run the active-image pause interaction and store the result in _return.

        Mirrors renpy.exports.pause closely, but intentionally omits
        renpy.ui.saybehavior so background clicks or Space/Enter presses do not
        dismiss the interaction before an active image is clicked or the
        optional delay expires. Also preserves non-boolean return values across
        roll-forward and checkpoints the interaction.

        Args:
            p: Parsed statement dictionary from parse_call_active.
        """
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
        """Validate the optional delay expression of a 'call active' statement.

        Args:
            p: Parsed statement dictionary from parse_call_active.
        """
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

    def get_render_content_bounds(r: Render) -> tuple[float, float, float, float] | None:
        """Compute the tight bounding box of visible child surfaces inside Render r.

        ImageReference and Transform renders often span the full screen size
        (e.g. 1920x1080) with the actual sprite blitted at an offset inside.
        Walking the Render tree and applying child offsets, reverse transform
        matrices, and clipping yields the true visual bounds of the sprite.

        Args:
            r: Render tree to inspect.

        Returns:
            tuple[float, float, float, float] | None: Bounding box (x0, y0, x1, y1)
            in r's coordinate space, or None if r contains no positive-area children.
        """
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

    def is_pixel_occluded(rv: Render, cx: float, cy: float) -> bool:
        """Check whether local point (cx, cy) in rv is clipped or covered by a sibling Render.

        Walks up the Render parent chain from rv to the root layer surface,
        transforming (cx, cy) at each level. Returns True if any ancestor clips
        the point out of bounds or if any later sibling Render (drawn on top of
        rv in zorder) has an opaque pixel at that position.

        Args:
            rv: ActiveImage Render surface.
            cx: Local X coordinate within rv.
            cy: Local Y coordinate within rv.

        Returns:
            bool: True if the point is clipped or occluded by an upper render node.
        """
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
        """Combine a standard button focus_mask with scene-layer occlusion testing.

        Ren'Py's built-in render_focus_mask only tests the button's own alpha
        mask. Wrapping it with is_pixel_occluded prevents clicks from passing
        through opaque non-interactive sprites or UI elements drawn in front.

        Args:
            rv: ActiveImage Render surface.
            base_mask: Render surface or callable returned by render_focus_mask.

        Returns:
            Callable[[int | float, int | float], bool]: Combined hit-test callable.
        """
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
        """Compute horizontal and vertical screen-clamped intervals for Focus f.

        When collapse[axis] is True for an ActiveImage, collapses that axis's
        interval to its center point so overlapping character sprites can be
        ordered by center position during keyboard navigation.

        Args:
            f: Focus entry from renpy.display.focus.focus_list.
            collapse: Pair of booleans (collapse_x, collapse_y).

        Returns:
            tuple[tuple[float, float], tuple[float, float]]: ((x0, x1), (y0, y1)).
        """
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
        """Select the nearest Focus candidate in the given arrow-key direction.

        Ren'Py's native focus_nearest requires strict non-overlapping intervals
        along the movement axis and biases toward wide UI bars when sprites
        overlap. By collapsing ActiveImage intervals to center points along the
        movement axis (and on both axes when crossing between sprites and UI
        widgets) and enforcing a 45-degree directional cone, this provides
        natural 2D arrow-key navigation across sprites and screen buttons.

        Args:
            from_focus: Currently focused Focus entry.
            candidates: List of eligible spatial Focus entries.
            spec: Direction tuple (axis, step) where axis is 0 (X) or 1 (Y) and
                step is 1 (right/down) or -1 (left/up).

        Returns:
            Focus | None: Best matching target Focus, or None if none qualify.
        """
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
        """Handle directional keyboard focus events when ActiveImages are on screen.

        If no ActiveImage is present in the current focus list, returns
        (False, None) so Ren'Py's default focus handler runs unchanged. When no
        widget is currently focused, selects the furthest ActiveImage in the
        pressed direction (matching Ren'Py's initial arrow-key behavior).

        Args:
            ev: Pygame event passed to Displayable.event.

        Returns:
            tuple[bool, Any]: (handled, result) where handled is True if the
            event was a directional focus key with active images present.
        """
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

    # --- Rollback and Roll-Forward State -------------------------------------

    class RollForwardState(NoRollback):
        """Non-rollback state container recording active-image clicks per AST node.

        Inherits from NoRollback so recorded click sequences survive when the
        player rolls back to an earlier dialogue node and can be replayed when
        rolling forward.
        """

        forward_clicks: dict[Any, list[Any]] = python_dict()
        active_rb_id: Any = None
        active_node_name: Any = None
        interacted_in_node: bool = False
        current_node_clicks: list[Any] = python_list()
        in_call_active: bool = False

    class ActiveRollForward(Null):
        """Underlay displayable handling directional focus and roll-forward replay.

        Registered in config.underlay so it receives input events across all
        interactions without altering screen layout. Intercepts directional
        focus keys when ActiveImages are present and replays recorded
        non-ending active-image click actions on roll-forward.
        """

        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            """Process directional focus keys and roll-forward events.

            Args:
                ev: Input event.
                x: Mouse X coordinate.
                y: Mouse Y coordinate.
                st: Shown time in seconds.

            Returns:
                Any: Interaction return value if triggered, otherwise None.
            """
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
        """Return the current rollback log identifier and AST node name.

        Returns:
            tuple[Any, Any]: Pair of (rollback_identifier, ast_node_name), or
            (None, None) if the rollback log is unavailable.
        """
        log = getattr(renpy.game, "log", None)
        return (log.current.identifier, log.current.context.current) if (log and log.current) else (None, None)

    def on_start_interact() -> None:
        """Track AST node transitions and prune stale roll-forward click history.

        Registered in config.start_interact_callbacks. When advancing to a new
        rollback entry without having clicked or rolled forward in the previous
        node, clears any saved clicks for that previous node once it leaves the
        forward log.
        """
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
        """Wrap an ActiveImage button action to record clicks and store _return.

        Ren'Py's built-in roll-forward only saves the final return value that
        ended an interaction. Wrapping the button action records each click in
        RollForwardState so side-effecting actions (like incrementing a counter
        during a dialogue line) replay on roll-forward, and stores non-None
        results in store._return.

        Args:
            action: Ren'Py Action or callable (or list of actions), or None.

        Returns:
            Callable[[], Any] | None: Wrapped action callable, or None.
        """
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

define config.show = active_images.show
define config.underlay += [active_images.ActiveRollForward()]
define config.start_interact_callbacks += [active_images.on_start_interact]
