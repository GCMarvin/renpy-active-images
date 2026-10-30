###############################################################################
## Active Images
###############################################################################
##
## Interactive sprites for Ren'Py.
##
## This library provides three script statements (`activate`, `deactivate`, and
## `call active`) that dynamically turn regular sprites into interactive
## buttons.
##
## SPDX-FileCopyrightText: 2026 GCMarvin
## SPDX-License-Identifier: MIT

python early in active_images:
    _constant = True

    import builtins
    import itertools
    from typing import Any, Callable, Iterable, Iterator

    from renpy.ast import Say, Show
    from renpy.display.behavior import Button, render_focus_mask
    from renpy.display.core import Displayable, IgnoreEvent
    from renpy.display.focus import Focus
    from renpy.display.image import ImageReference
    from renpy.display.layout import Null
    from renpy.display.motion import ATLTransform, Transform
    from renpy.display.render import Render
    from renpy.display.scenelists import SceneListEntry
    from renpy.game import CallException, JumpException
    from renpy.lexer import Lexer
    from renpy.parser import parse_image_specifier, parse_simple_expression_list

    BUTTON_CLAUSES: frozenset[str] = frozenset({
        "action",
        "alternate",
        "hovered",
        "unhovered",
        "selected",
        "sensitive",
        "keysym",
        "alternate_keysym",
        "focus_mask",
    })
    FOCUS_DIRECTIONS: dict[str, tuple[int, int]] = {
        "focus_right": (0, 1),
        "focus_left": (0, -1),
        "focus_down": (1, 1),
        "focus_up": (1, -1),
    }

    # --- ActiveImage Displayable ---------------------------------------------


    class ActiveImage(Button):
        """Interactive scene-list sprite wrapper inheriting from
        Ren'Py's Button.

        Positioned in the scene-list displayable chain between outer
        scene transforms (such as placement or flipping) and inner
        button transforms (such as hover/idle ATL blocks or
        style-prefixed properties). This split lets outer transforms
        position the sprite on screen while ActiveImage intercepts
        mouse/keyboard events and forwards hover/idle state only to the
        inner button transforms.
        """

        def __init__(
            self,
            child: Displayable | None = None,
            layer_key: tuple[str, str] | None = None,
            **btn_kwargs: Any,
        ) -> None:
            """Initialize the active image button wrapper.

            Defaults action to NullAction() and focus_mask to True so
            transparent sprite padding ignores pointer events, while
            keeping action and alternate in their raw picklable form so
            saving and rollback work cleanly.

            Args:
                child (Displayable | None, default=None): Inner
                    displayable chain (button transforms and leaf
                    image).
                layer_key (tuple[str, str] | None, default=None):
                    Optional (layer, tag) pair identifying this
                    scene-list entry.
                **btn_kwargs (Any): Keyword arguments matching standard
                    Button clauses.
            """
            self.layer_key = layer_key
            self.btn_kwargs = {
                "action": renpy.store.NullAction(),
                "focus_mask": True,
                **btn_kwargs,
            }
            focus = f"active_image:{layer_key[0]}:{layer_key[1]}" if layer_key else None
            super().__init__(child, style="default", focus=focus, **self.btn_kwargs)

        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            """Delegate input events to Button.event with
            click-recording wrappers applied.

            Wrapping clicked and alternate only during super().event
            (and restoring them before executing the action) keeps the
            stored Action objects picklable for saving and rollback
            while recording clicks for roll-forward replay.

            Args:
                ev (Any): Pygame input event.
                x (int): Local X coordinate relative to this
                    displayable.
                y (int): Local Y coordinate relative to this
                    displayable.
                st (float): Shown time in seconds.

            Returns:
                Any: Interaction result from Button.event, or None if
                    unhandled.
            """
            orig = (self.clicked, self.alternate)
            try:
                self.clicked, self.alternate = wrap_click(self.clicked, self), wrap_click(
                    self.alternate, self
                )
                return super().event(ev, x, y, st)
            finally:
                self.clicked, self.alternate = orig

        def _change_transform_child(self, child: Displayable) -> Displayable:
            """Clone this ActiveImage around a new leaf child during
            SceneLists updates.

            Displayable._change_transform_child normally returns child
            directly, which would discard a Button wrapper when Ren'Py's
            SceneLists.add updates an image without new transforms.
            Overriding this method preserves both the ActiveImage
            wrapper and any inner button transforms.

            Args:
                child (Displayable): New replacement displayable passed
                    by SceneLists.add.

            Returns:
                Displayable: Either child if already wrapped in
                    ActiveImage, or a new ActiveImage preserving inner
                    button transforms and focus state.
            """
            if isinstance(child, ActiveImage):
                return child
            if self.child is not None:
                self.child.get_placement()
            _, old_btn, _ = split_transform_chain(self.child)
            rv = ActiveImage(
                chain_transforms(old_btn, child, copy=True),
                layer_key=self.layer_key,
                **self.btn_kwargs,
            )
            transfer_active_state(self, rv)
            return rv

        def visit(self) -> list[Displayable | None]:
            """Clear any temporary 'replaced$' SceneListEntry once the
            replacement entry is active.

            After SceneLists.add inserts a replacement entry updated via
            _change_transform_child, it calls thing.visit_all(lambda d:
            None). Removing the 'replaced$' entry here cleans it up
            synchronously without requiring external state or extra
            callbacks.

            Returns:
                list[Displayable | None]: Child displayables visited by
                    Button.visit.
            """
            if self.layer_key is not None and (ctx := renpy.game.context()) is not None:
                if get_sle(*self.layer_key) is not None:
                    ctx.scene_lists.remove_hide_replaced(*self.layer_key)
            return super().visit()

        def _handles_event(self, event: str) -> bool:
            """Report that ActiveImage handles 'replaced' events during
            SceneLists.hide_or_replace.
            """
            return event == "replaced"

        def _hide(self, st: float, at: float, kind: str) -> Displayable | None:
            """Preserve this ActiveImage in a 'replaced$' entry during
            SceneLists replacement.

            Called by SceneLists.hide_or_replace before an entry is
            removed or replaced. Returning self when kind is 'replaced'
            instructs SceneLists.hide_or_replace to keep a temporary
            'replaced$' entry on the layer so either ActiveImage.visit
            (when no new transforms are applied) or sync_active_images
            (when new transforms are applied) can clean up or re-wrap
            the replacement entry.

            Args:
                st (float): Shown time in seconds.
                at (float): Animation time in seconds.
                kind (str): Hide operation kind ('hide', 'replaced', or
                    'cancel').

            Returns:
                Displayable | None: Self when kind is 'replaced',
                    otherwise None.
            """
            if kind == "replaced" and self.layer_key is not None:
                if self.child is not None:
                    self.child.get_placement()
                return self
            return None

        def get_placement(self) -> tuple:
            """Delegate placement queries to the wrapped child
            displayable.

            Button inherits Window placement defaults, which would
            override an ImageReference's image_placement style unless
            delegated to child.

            Returns:
                tuple: Placement tuple (xpos, ypos, xanchor, yanchor,
                    xoffset, yoffset, subpixel) from the child
                    displayable.
            """
            return (
                self.child.get_placement() if self.child is not None else super().get_placement()
            )

        def set_style_prefix(self, prefix: str, root: bool) -> None:
            """Propagate button style prefixes only when triggered by
            ActiveImage itself.

            Outer scene transforms call set_style_prefix with
            root=False; ignoring those prevents outer transforms from
            overwriting the button's own hover_ or idle_ prefix. Any
            descendant still referencing Ren'Py's shared default_style
            is given its own Style('default') instance first so
            default_style is never mutated.

            Args:
                prefix (str): Style prefix string (such as 'hover_' or
                    'idle_').
                root (bool): True when initiated directly by this
                    button's focus state.
            """
            if root:
                default_style = renpy.display.displayable.default_style
                self.visit_all(
                    lambda d: (
                        setattr(d, "style", renpy.style.Style("default"))
                        if d.style is default_style
                        else None
                    )
                )
                super().set_style_prefix(prefix, root)

        def set_transform_event(self, event: str) -> None:
            """Forward scene-list lifecycle transform events to inner
            transforms selectively.

            Container does not forward 'show', 'replace', or 'replaced'
            to its children, whereas unconditional forwarding would
            overwrite 'hover' or 'idle' on inner button transforms that
            do not handle scene events. This override forwards scene
            lifecycle events only when an inner transform explicitly
            defines an ATL handler for them.

            Args:
                event (str): Transform event name (such as 'show',
                    'replace', 'hover', 'idle').
            """
            super().set_transform_event(event)
            if event in ("show", "replace", "replaced") and self.child is not None:
                if any(transform_handles_event(n, (event,)) for n in iter_chain(self.child)):
                    self.child.set_transform_event(event)

        def focus(self, default: bool = False) -> Any:
            """Handle focus gain and schedule a redraw across the inner
            transform chain.

            Args:
                default (bool, default=False): True when restoring
                    initial focus without firing hovered.

            Returns:
                Any: Result of the hovered action if executed, otherwise
                    None.
            """
            rv = super().focus(default)
            self.visit_all(lambda d: renpy.display.render.redraw(d, 0))
            return rv

        def unfocus(self, default: bool = False) -> None:
            """Handle focus loss while preserving initial
            'show'/'replace' ATL events.

            Button.unfocus unconditionally sets the 'idle' transform
            event on its child, which would abort an inner transform's
            'on show:' or 'on replace:' animation during initial scene
            setup (default=True). When such an event is pending, this
            calls Window.unfocus directly.

            Args:
                default (bool, default=False): True when setting initial
                    unfocused state without firing unhovered.
            """
            ev = getattr(self.child, "transform_event", None)
            keep_ev = (
                default
                and self.is_sensitive()
                and ev in ("show", "replace")
                and any(transform_handles_event(n, (ev,)) for n in iter_chain(self.child))
            )
            (super(Button, self) if keep_ev else super()).unfocus(default)
            self.visit_all(lambda d: renpy.display.render.redraw(d, 0))

        def render(self, width: int, height: int, st: float, at: float) -> Render:
            """Render the child sprite directly and register an
            occlusion-aware focus mask.

            Button.render uses Window layout (adding window padding,
            background, and sizing), which is unsuitable for scene-list
            sprites. This method renders the child directly like an
            image while registering a focus rectangle tightened to the
            visible sprite bounds and wrapped with scene-layer occlusion
            checking.

            Args:
                width (int): Available horizontal space in pixels.
                height (int): Available vertical space in pixels.
                st (float): Shown time in seconds.
                at (float): Animation time in seconds.

            Returns:
                Render: Render surface containing the child sprite and
                    focus region.
            """
            surf = renpy.display.render.render(self.child, width, height, st, at)
            sw, sh = surf.get_size()
            rv = Render(sw, sh)
            rv.blit(surf, (0, 0))
            self.offsets = [(0, 0)]
            if self.is_sensitive():
                b = get_render_content_bounds(surf)
                bx0, by0, bx1, by1 = (
                    (max(0.0, b[0]), max(0.0, b[1]), min(float(sw), b[2]), min(float(sh), b[3]))
                    if b
                    else (0.0, 0.0, float(sw), float(sh))
                )
                base_mask = render_focus_mask(self.style.focus_mask, sw, sh, st, at, surf)
                mask = (
                    (
                        lambda mx, my: (
                            is_render_opaque(base_mask, mx, my)
                            if isinstance(base_mask, Render)
                            else base_mask(mx, my)
                        )
                        and not is_pixel_occluded(rv, mx, my)
                    )
                    if base_mask is not None
                    else None
                )
                fmx, fmy = (0, 0) if mask is not None else (None, None)
                rv.add_focus(
                    self, None, bx0, by0, max(1.0, bx1 - bx0), max(1.0, by1 - by0), fmx, fmy, mask
                )
            return rv


    # --- Transform Hierarchy & SceneLists Integration ------------------------


    def iter_chain(d: Displayable | None) -> Iterator[Displayable]:
        """Yield each displayable along a single-child wrapper chain.

        Args:
            d (Displayable | None): Root displayable of the chain.

        Yields:
            Displayable: Each displayable from root to leaf.
        """
        while d is not None:
            yield d
            d = getattr(d, "child", None)


    def get_sle(layer: str, key: str) -> SceneListEntry | None:
        """Return the latest SceneListEntry for tag key on layer, or
        None.

        Args:
            layer (str): Scene layer name.
            key (str): Image tag to look up.

        Returns:
            SceneListEntry | None: Matching entry on the layer if
                present.
        """
        layers = renpy.game.context().scene_lists.layers.get(layer, ())
        return next((sle for sle in reversed(layers) if sle.tag == key), None)


    def find_active_image(d: Displayable | None) -> ActiveImage | None:
        """Find the ActiveImage instance inside a displayable wrapper
        chain.

        Args:
            d (Displayable | None): Root displayable to search.

        Returns:
            ActiveImage | None: The wrapped ActiveImage if present, else
                None.
        """
        return next((n for n in iter_chain(d) if isinstance(n, ActiveImage)), None)


    def transform_handles_event(d: Displayable | None, events: Iterable[str]) -> bool:
        """Check whether an ATLTransform's compiled block handles any of
        events.

        Args:
            d (Displayable | None): Displayable (typically an
                ATLTransform) to inspect.
            events (Iterable[str]): Transform event names to check for.

        Returns:
            bool: True if d is an ATLTransform with a handler for any
                given event.
        """
        block = (d.get_block() or d.compile()) if isinstance(d, ATLTransform) else None
        return block is not None and any(block._handles_event(ev) for ev in events)


    def is_button_transform(t: Any) -> bool:
        """Classify whether a Transform belongs inside ActiveImage
        (button transform).

        A transform is treated as an inner button transform if it
        responds to button container events (such as 'hover' or 'idle'
        in config.containers_pass_transform_events) or receives keyword
        arguments. Otherwise it is treated as an outer scene transform.

        Args:
            t (Any): Transform to classify.

        Returns:
            bool: True if t should be placed inside ActiveImage, False
                if outside.
        """
        args = getattr(t, "arguments", None)
        return bool(args and any(k != "" for k in args)) or transform_handles_event(
            t, renpy.config.containers_pass_transform_events
        )


    def split_transform_chain(
        d: Displayable | None,
    ) -> tuple[list[Transform], list[Transform], Displayable | None]:
        """Partition a displayable chain into outer scene transforms,
        inner button transforms, and leaf.

        Args:
            d (Displayable | None): Displayable wrapped in zero or more
                Transforms (and optionally an ActiveImage).

        Returns:
            tuple[list[Transform], list[Transform], Displayable | None]:
                A 3-tuple of (scene_transforms, button_transforms,
                leaf_displayable) ordered from innermost to outermost so
                chain_transforms can rebuild them bottom-up.
        """
        scene_chain: list[Transform] = []
        button_chain: list[Transform] = []
        in_button = False
        curr = d
        while curr is not None:
            if isinstance(curr, ActiveImage):
                in_button = True
                curr = curr.child
            elif isinstance(curr, Transform) and curr.child is not None:
                (button_chain if (in_button or is_button_transform(curr)) else scene_chain).append(
                    curr
                )
                curr = curr.child
            else:
                break
        return scene_chain[::-1], button_chain[::-1], curr


    def chain_transforms(
        transforms: list[Transform], leaf: Displayable, copy: bool = False
    ) -> Displayable:
        """Wrap leaf in a sequence of Transforms from innermost to
        outermost.

        Args:
            transforms (list[Transform]): List of Transforms ordered
                from innermost to outermost.
            leaf (Displayable): Innermost displayable to wrap.
            copy (bool, default=False): If True, copy each Transform
                (preserving its state) before attaching.

        Returns:
            Displayable: Root of the constructed transform chain.
        """
        curr: Displayable = leaf
        for t in transforms:
            if copy:
                t.get_placement()
                t = t.copy()
            t.set_child(curr, duplicate=False)
            t._unique()
            curr = t
        return curr


    def transfer_active_state(old_widget: ActiveImage, new_widget: ActiveImage) -> None:
        """Transfer focus replacement mapping and hover/idle state to a
        replacement ActiveImage.

        Registers old_widget -> new_widget in
        renpy.display.focus.replaced_by so Ren'Py's focus system
        recognizes new_widget as the same logical button across
        interactions, and applies the matching focus/unfocus state.

        Args:
            old_widget (ActiveImage): Previous ActiveImage being
                replaced on the layer.
            new_widget (ActiveImage): Newly created ActiveImage
                replacing it.
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


    def wrap_sle_active(
        layer: str,
        key: str,
        sle: Any,
        old_active: ActiveImage | None = None,
        old_btn: list[Transform] = [],
        new_btn: list[Transform] = [],
        btn_kwargs: dict[str, Any] | None = None,
        op: str = "activate",
    ) -> None:
        """Wrap or unwrap a SceneListEntry's displayable with an
        ActiveImage.

        Partitions the entry's transform chain into outer scene
        transforms and inner button transforms, transitions inner button
        transform state via SceneLists.transform_state, and either
        inserts an ActiveImage between the outer and inner chains (when
        activating or updating) or strips ActiveImage while preserving
        all transforms (when deactivating).

        Args:
            layer (str): Scene layer name.
            key (str): Image tag or key on the layer.
            sle (Any): SceneListEntry on the layer to update in place.
            old_active (ActiveImage | None, default=None): Previous
                ActiveImage on this tag, if any.
            old_btn (list[Transform], default=[]): Previous inner button
                transforms from old_active or prior entry.
            new_btn (list[Transform], default=[]): New inner button
                transforms to apply.
            btn_kwargs (dict[str, Any] | None, default=None): Evaluated
                Button keyword arguments from an 'activate' statement.
            op (str, default="activate"): Operation mode ('activate' or
                'deactivate').
        """
        sls = renpy.exports.scene_lists()
        sle.displayable.get_placement()
        if old_active is not None and old_active.child is not None:
            old_active.child.get_placement()
            if not old_btn:
                _, old_btn, _ = split_transform_chain(old_active.child)

        new_scene, sle_btn, leaf = split_transform_chain(sle.displayable)
        new_btn = new_btn or sle_btn
        if leaf is None:
            return
        old_inner = chain_transforms(old_btn, leaf, copy=True) if old_btn else None
        inner_d = (
            sls.transform_state(old_inner, chain_transforms(new_btn, leaf), execution=True)
            if new_btn
            else (old_inner or leaf)
        )

        merged_kwargs = {**(old_active.btn_kwargs if old_active else {}), **(btn_kwargs or {})}
        active_img = ActiveImage(inner_d, layer_key=(layer, key), **merged_kwargs)
        if op == "deactivate":
            was_focused = (
                old_active is not None and renpy.display.focus.get_focused() is old_active
            )
            active_img.unfocus(default=not was_focused)
            sle.displayable = chain_transforms(new_scene, active_img.child)
            return

        sle.displayable = (
            chain_transforms(new_scene, active_img) if new_scene else Transform(child=active_img)
        )
        if key in sls.at_list[layer]:
            sls.at_list[layer][key] = [
                t for t in sls.at_list[layer][key] if not is_button_transform(t)
            ]
        if old_active is not None:
            transfer_active_state(old_active, active_img)
        else:
            active_img.unfocus(default=True)


    def sync_active_images(_stmt: str | None = None) -> None:
        """Re-wrap any active images replaced by native 'show'
        statements with transforms.

        When a native 'show' statement without 'at' or ATL replaces an
        active image, SceneLists.add calls
        ActiveImage._change_transform_child and ActiveImage.visit cleans
        up the temporary 'replaced$' entry. When a native 'show'
        statement includes 'at' or ATL, SceneLists.hide_or_replace
        retains the old ActiveImage in a 'replaced$' SceneListEntry so
        this callback can re-wrap the updated entry and transfer focus
        state.

        Args:
            _stmt (str | None, default=None): Optional statement name
                passed by config.statement_callbacks.
        """
        if (ctx := renpy.game.context()) is None:
            return
        sls = ctx.scene_lists
        for layer, entries in list(sls.layers.items()):
            for sle in list(entries):
                if (
                    sle.tag
                    and sle.tag.startswith("replaced$")
                    and (old_active := find_active_image(sle.displayable)) is not None
                ):
                    key = sle.tag.partition("$")[2]
                    sls.remove_hide_replaced(layer, key)
                    if (new_sle := get_sle(layer, key)) is not None and find_active_image(
                        new_sle.displayable
                    ) is None:
                        if getattr(new_sle.displayable, "transform_event", None) == "replace":
                            wrap_sle_active(
                                layer, key, new_sle, old_active=old_active, op="activate"
                            )


    # --- Custom Statements: activate, deactivate, and call active ------------


    def parse_activate_or_deactivate(
        l: Lexer, allow_button_clauses: bool = True
    ) -> dict[str, Any]:
        """Parse an 'activate' or 'deactivate' statement into an imspec
        and button clauses.

        Uses Ren'Py's native parse_image_specifier for the leading image
        and show clauses, then continues parsing interspersed button
        clauses (for 'activate') and trailing show/with clauses in any
        order.

        Args:
            l (Lexer): Ren'Py script lexer positioned after the
                statement keyword.
            allow_button_clauses (bool, default=True): True for
                'activate', False for 'deactivate'.

        Returns:
            dict[str, Any]: Parsed statement dictionary containing
                'imspec', 'button_clauses', and 'with_expr'.
        """
        stmt = "activate" if allow_button_clauses else "deactivate"
        name, expression, tag, at_list, layer, zorder, behind = parse_image_specifier(l)
        clauses: dict[str, Any] = {
            "at": list(at_list),
            "as": tag,
            "onlayer": layer,
            "zorder": zorder,
            "behind": list(behind or ()),
            "with": None,
        }
        button_clauses: dict[str, str] = {}

        while not l.eol() and not l.match(":"):
            pos = l.checkpoint()
            if not (word := l.word()):
                l.error(f"expected clause in {stmt} statement.")
            if word in clauses:
                if clauses[word]:
                    l.error(f"duplicate '{word}' clause in {stmt} statement.")
                if word == "at":
                    clauses["at"] = list(parse_simple_expression_list(l, image=True))
                elif word == "behind":
                    while True:
                        clauses["behind"].append(l.require(l.name))
                        if not l.match(","):
                            break
                elif word == "as":
                    clauses["as"] = l.require(l.name)
                elif word == "onlayer":
                    clauses["onlayer"] = l.require(l.image_name_component)
                else:
                    clauses[word] = l.require(l.simple_expression)
            elif allow_button_clauses and word in BUTTON_CLAUSES:
                if word in button_clauses:
                    l.error(f"duplicate '{word}' clause in {stmt} statement.")
                button_clauses[word] = l.require(l.simple_expression)
            else:
                l.revert(pos)
                l.error(f"unexpected clause '{word}' in {stmt} statement.")

        imspec = (
            name,
            expression,
            clauses["as"],
            clauses["at"],
            clauses["onlayer"],
            clauses["zorder"],
            clauses["behind"],
        )
        return {"imspec": imspec, "button_clauses": button_clauses, "with_expr": clauses["with"]}


    def execute_activate_or_deactivate(
        p: dict[str, Any], atl: Any = None, op: str = "activate"
    ) -> None:
        """Execute a parsed 'activate' or 'deactivate' statement.

        Unwraps any existing ActiveImage or inner button transforms on
        the target tag so Ren'Py's native show transitions outer scene
        transforms cleanly, calls renpy.config.show, and wraps or
        unwraps the resulting entry via wrap_sle_active.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                parse_activate_or_deactivate.
            atl (Any, default=None): Optional compiled ATLTransform
                attached to the statement.
            op (str, default="activate"): Operation type ('activate' or
                'deactivate').
        """
        if (with_expr := p["with_expr"]) is not None:
            renpy.exports.with_statement(None)

        name, expression, tag, at_list, layer, zorder, behind = p["imspec"]
        zorder = renpy.python.py_eval(zorder) if zorder is not None else None
        what = None
        if expression is not None:
            what = renpy.python.py_eval(expression)
            if not renpy.config.old_show_expression:
                if isinstance(what, str):
                    name = what
                else:
                    if tag is None:
                        tag = next(
                            t
                            for i in itertools.count()
                            if not renpy.exports.showing(t := f"_show_expression_{i}", layer)
                        )
                    name = tag
            what = renpy.easy.displayable(what)

        eval_at = [renpy.python.py_eval(i) for i in at_list]
        name = name if isinstance(name, tuple) else tuple(name.split())
        key = tag or name[0]
        layer = renpy.exports.default_layer(layer, key, bool(expression) and (tag is None))
        sls = renpy.exports.scene_lists()
        old_sle = get_sle(layer, key)

        if op == "deactivate" and old_sle is None:
            if with_expr is not None:
                renpy.exports.with_statement(renpy.python.py_eval(with_expr))
            return

        old_active = find_active_image(old_sle.displayable) if old_sle is not None else None
        old_btn: list[Transform] = []
        if old_sle is not None:
            old_sle.displayable.get_placement()
            old_scene, old_btn, old_leaf = split_transform_chain(old_sle.displayable)
            if what is None and old_leaf is not None:
                if (
                    isinstance(old_leaf, ImageReference)
                    and isinstance(old_leaf.name, tuple)
                    and old_leaf.name
                ):
                    if (
                        name[0] == key != old_leaf.name[0]
                        and renpy.display.image.images.get(name) is None
                        and not renpy.display.image.image_attributes.get(key)
                    ):
                        name, tag = (old_leaf.name[0],) + name[1:], key
                elif not isinstance(old_leaf, ImageReference) and name == (key,):
                    what = old_leaf
            if (old_active is not None or old_btn) and old_leaf is not None:
                sle_copy = old_sle.copy()
                sle_copy.displayable = chain_transforms(old_scene, old_leaf, copy=True)
                sls.layers[layer][sls.layers[layer].index(old_sle)] = sle_copy

        scene_at = [t for t in eval_at if not is_button_transform(t)]
        btn_at = [
            t() if isinstance(t, Transform) else t for t in eval_at if is_button_transform(t)
        ]
        if atl is not None and is_button_transform(atl):
            btn_at.append(atl)
            scene_atl = None
        else:
            scene_atl = atl

        btn_kwargs = {kw: renpy.python.py_eval(expr) for kw, expr in p["button_clauses"].items()}
        renpy.config.show(
            name,
            at_list=scene_at,
            layer=layer,
            what=what,
            zorder=zorder,
            tag=tag,
            behind=behind,
            atl=scene_atl,
        )
        sls.remove_hide_replaced(layer, key)
        if (new_sle := get_sle(layer, key)) is not None:
            wrap_sle_active(
                layer,
                key,
                new_sle,
                old_active=old_active,
                old_btn=old_btn,
                new_btn=btn_at,
                btn_kwargs=btn_kwargs,
                op=op,
            )

        if with_expr is not None:
            renpy.exports.with_statement(renpy.python.py_eval(with_expr))


    def lint_activate_or_deactivate(p: dict[str, Any], stmt: str = "activate") -> None:
        """Validate image specifiers and clause expressions during
        'renpy lint'.

        Delegates image, layer, and at-list checking to Ren'Py's native
        check_show (treating single-word tags already registered in
        renpy.lint.image_prefixes as valid tag references), then
        validates all Python expressions in button, zorder, and with
        clauses.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                parse_activate_or_deactivate.
            stmt (str, default="activate"): Statement name ('activate'
                or 'deactivate') for lint messages.
        """
        name, expression, tag, at_list, layer, zorder, behind = p["imspec"]
        is_known_tag = len(name) == 1 and name[0] in renpy.lint.image_prefixes
        imspec = (
            name,
            expression or ("True" if is_known_tag else None),
            tag,
            at_list,
            layer,
            zorder,
            behind,
        )
        renpy.lint.check_show(Show((None, 0), imspec), False)
        for kw, expr in (
            *p["button_clauses"].items(),
            ("zorder", zorder),
            ("with", p["with_expr"]),
        ):
            if expr is not None:
                renpy.lint.try_eval(f"the {kw} clause of a {stmt} statement", expr)


    def parse_call_active(l: Lexer) -> dict[str, Any]:
        """Parse a 'call active' statement and its optional single-Say
        subblock.

        When a colon subblock is provided, verifies that it contains a
        single Say statement and sets its interact=False so the dialogue
        line is displayed on screen while post_execute_call_active runs
        the modal active-image interaction.

        Args:
            l (Lexer): Ren'Py script lexer positioned after 'call
                active'.

        Returns:
            dict[str, Any]: Dictionary with 'delay' expression and
                'subparse' block.
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
        """Prepare skipping, rollback, and window state before 'call
        active' runs.

        Defers rollback checkpointing until post_execute_call_active so
        that any nested non-interactive Say statement and the subsequent
        active-image interaction share a single rollback checkpoint.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                parse_call_active.
        """
        renpy.exports.choice_for_skipping()
        renpy.exports.stop_skipping()
        renpy.store._begin_rollback = False
        if p["subparse"] is None:
            renpy.store._window_auto = True


    def post_execute_call_active(p: dict[str, Any]) -> None:
        """Run the active-image pause interaction and store the result
        in _return.

        Mirrors renpy.exports.pause closely, but intentionally omits
        renpy.ui.saybehavior so background clicks or Space/Enter presses
        do not dismiss the interaction before an active image is clicked
        or the optional delay expires. Also preserves non-boolean return
        values across roll-forward and checkpoints the interaction.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                parse_call_active.
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
        try:
            rv = renpy.ui.interact(
                mouse="pause", type="call_active", roll_forward=roll_forward, pause=delay
            )
        except (JumpException, CallException) as e:
            rv = e

        renpy.exports.checkpoint(
            rv, keep_rollback=True, hard=renpy.config.pause_after_rollback or (delay is None)
        )
        if renpy.config.implicit_with_none:
            renpy.game.interface.do_with(None, None)
        if isinstance(rv, (JumpException, CallException)):
            raise rv
        renpy.store._return = rv


    def lint_call_active(p: dict[str, Any]) -> None:
        """Validate the optional delay expression of a 'call active'
        statement.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                parse_call_active.
        """
        if p["delay"] is not None:
            renpy.lint.try_eval("the delay of a call active statement", p["delay"])


    for _stmt, _allow_btn in (("activate", True), ("deactivate", False)):
        renpy.register_statement(
            _stmt,
            parse=lambda l, b=_allow_btn: parse_activate_or_deactivate(l, allow_button_clauses=b),
            execute=lambda p, atl=None, op=_stmt: execute_activate_or_deactivate(p, atl, op=op),
            predict=lambda p: (renpy.ast.predict_imspec(p["imspec"]), [])[1],
            lint=lambda p, s=_stmt: lint_activate_or_deactivate(p, stmt=s),
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
        """Compute the tight bounding box of visible child surfaces
        inside Render r.

        ImageReference and Transform renders often span the full screen
        size (e.g. 1920x1080) with the actual sprite blitted at an
        offset inside. Walking the Render tree and applying child
        offsets, reverse transform matrices, and clipping yields the
        true visual bounds of the sprite.

        Args:
            r (Render): Render tree to inspect.

        Returns:
            tuple[float, float, float, float] | None: Bounding box (x0,
                y0, x1, y1) in r's coordinate space, or None if r
                contains no positive-area children.
        """
        bx0 = by0 = float("inf")
        bx1 = by1 = float("-inf")
        rev = getattr(r, "reverse", None)
        has_matrix = rev is not None and (rev.xdx, rev.xdy, rev.ydx, rev.ydy) != (
            1.0,
            0.0,
            0.0,
            1.0,
        )
        for child, xo, yo, _, _ in r.children:
            if isinstance(child, Render):
                cb = get_render_content_bounds(child)
            elif hasattr(child, "get_bounding_rect"):
                rx, ry, rw, rh = child.get_bounding_rect()
                cb = (
                    (float(rx), float(ry), float(rx + rw), float(ry + rh))
                    if (rw > 0 and rh > 0)
                    else None
                )
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
                cx0, cy0, cx1, cy1 = min(xs), min(ys), max(xs), max(ys)
            bx0, by0 = min(bx0, cx0 + xo), min(by0, cy0 + yo)
            bx1, by1 = max(bx1, cx1 + xo), max(by1, cy1 + yo)

        if getattr(r, "xclipping", False):
            bx0, bx1 = max(0.0, bx0), min(float(r.width), bx1)
        if getattr(r, "yclipping", False):
            by0, by1 = max(0.0, by0), min(float(r.height), by1)
        return (bx0, by0, bx1, by1) if (bx0 < bx1 and by0 < by1) else None


    def is_render_opaque(r: Render, x: float, y: float) -> bool:
        """Check whether local point (x, y) inside Render r hits an
        opaque pixel.

        Ren'Py's built-in Render.is_pixel_opaque returns True for the
        entire bounding box whenever a Render node has a non-identity
        reverse matrix (such as a scaled or flipped Transform inside a
        LayeredImage or button transform). Walking the Render tree and
        transforming coordinates through each node's forward matrix down
        to the leaf surfaces preserves exact per-pixel hit-testing
        across transformed children.

        Args:
            r (Render): Render tree to test.
            x (float): Local X coordinate in r's space.
            y (float): Local Y coordinate in r's space.

        Returns:
            bool: True if (x, y) lands on a visible pixel with non-zero
                alpha.
        """
        if (
            (r.xclipping and not (0 <= x < r.width))
            or (r.yclipping and not (0 <= y < r.height))
            or getattr(r, "alpha", 1.0) <= 0
        ):
            return False
        for child, xo, yo, _, _ in r.children:
            cx, cy = (
                r.forward.transform(x - xo, y - yo) if r.forward is not None else (x - xo, y - yo)
            )
            if isinstance(child, Render):
                if is_render_opaque(child, cx, cy):
                    return True
            elif (
                0 <= (ix := int(cx)) < child.get_size()[0]
                and 0 <= (iy := int(cy)) < child.get_size()[1]
            ):
                if hasattr(child, "get_at"):
                    if child.get_at((ix, iy))[3] > 0:
                        return True
                else:
                    sub = Render(1, 1)
                    sub.blit(child.subsurface((ix, iy, 1, 1)), (0, 0))
                    if renpy.display.draw.is_pixel_opaque(sub):
                        return True
        return False


    def is_pixel_occluded(rv: Render, cx: float, cy: float) -> bool:
        """Check whether local point (cx, cy) in rv is clipped or
        covered by a sibling Render.

        Walks up the Render parent chain from rv to the root layer
        surface, transforming (cx, cy) at each level. Returns True if
        any ancestor clips the point out of bounds or if any later
        sibling Render (drawn on top of rv in zorder) has an opaque
        pixel at that position.

        Args:
            rv (Render): ActiveImage Render surface.
            cx (float): Local X coordinate within rv.
            cy (float): Local Y coordinate within rv.

        Returns:
            bool: True if the point is clipped or occluded by an upper
                render node.
        """
        curr = rv
        while found := next(
            (
                (p, i, xo, yo)
                for p in curr.parents
                for i, (c, xo, yo, f, _) in enumerate(p.children)
                if c is curr and f
            ),
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
                    if is_render_opaque(sib, sx, sy):
                        return True
            curr, cx, cy = parent, px, py
        return False


    def get_focus_spans(
        f: Focus, collapse: tuple[bool, bool] = (False, False)
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Compute horizontal and vertical screen-clamped intervals for
        Focus f.

        When collapse[axis] is True for an ActiveImage, collapses that
        axis's interval to its center point so overlapping character
        sprites can be ordered by center position during keyboard
        navigation.

        Args:
            f (Focus): Focus entry from renpy.display.focus.focus_list.
            collapse (tuple[bool, bool], default=(False, False)):
                Pair of booleans (collapse_x, collapse_y).

        Returns:
            tuple[tuple[float, float], tuple[float, float]]: ((x0, x1),
                (y0, y1)).
        """
        x, y, w, h = f.inset_rect()
        if not isinstance(f.widget, ActiveImage):
            return ((float(x), float(x + w)), (float(y), float(y + h)))
        spans = []
        for orig, size, lim, col in zip(
            (x, y), (w, h), (renpy.config.screen_width, renpy.config.screen_height), collapse
        ):
            p0, p1 = float(orig), float(orig + size)
            if (c0 := max(0.0, p0)) < (c1 := min(float(lim), p1)):
                p0, p1 = c0, c1
            spans.append(((p0 + p1) * 0.5, (p0 + p1) * 0.5) if col else (p0, p1))
        return (spans[0], spans[1])


    def find_directional_focus(
        from_focus: Focus, candidates: list[Focus], spec: tuple[int, int]
    ) -> Focus | None:
        """Select the nearest Focus candidate in the given arrow-key
        direction.

        Ren'Py's native focus_nearest requires strict non-overlapping
        intervals along the movement axis and biases toward wide UI bars
        when sprites overlap. By collapsing ActiveImage intervals to
        center points along the movement axis (and on both axes when
        crossing between sprites and UI widgets) and enforcing a
        45-degree directional cone, this provides natural 2D arrow-key
        navigation across sprites and screen buttons.

        Args:
            from_focus (Focus): Currently focused Focus entry.
            candidates (list[Focus]): List of eligible spatial Focus
                entries.
            spec (tuple[int, int]): Direction tuple (axis, step) where
                axis is 0 (X) or 1 (Y) and step is 1 (right/down) or -1
                (left/up).

        Returns:
            Focus | None: Best matching target Focus, or None if none
                qualify.
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
            main_gap = (nm0 - om1) if step > 0 else (om0 - nm1)
            if (nmc - omc) * step <= 0 or main_gap < 0:
                continue
            perp_gap = max(0.0, np0 - op1, op0 - np1)
            perp_diff = abs(npc - opc)
            if (cross and perp_gap > main_gap) or (
                from_active and to_active and perp_diff > main_gap
            ):
                continue
            score = (main_gap**2 + (perp_gap * penalty) ** 2, perp_diff)
            if score < best_score:
                best, best_score = f, score
        return best


    def handle_active_spatial_focus(ev: Any) -> tuple[bool, Any]:
        """Handle directional keyboard focus events when ActiveImages
        are on screen.

        If no ActiveImage is present in the current focus list, returns
        (False, None) so Ren'Py's default focus handler runs unchanged.
        When no widget is currently focused, selects the furthest
        ActiveImage in the pressed direction (matching Ren'Py's initial
        arrow-key behavior).

        Args:
            ev (Any): Pygame event passed to Displayable.event.

        Returns:
            tuple[bool, Any]: (handled, result) where handled is True if
                the event was a directional focus key with active images
                present.
        """
        if renpy.game.preferences.self_voicing:
            return False, None
        direction = next(
            (d for d in FOCUS_DIRECTIONS if renpy.display.behavior.map_event(ev, d)), None
        )
        if direction is None:
            return False, None

        spatial_focuses = [
            f
            for f in renpy.display.focus.focus_list
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
            (
                f
                for f in spatial_focuses
                if current is not None
                and f.widget is current
                and f.arg == renpy.display.focus.argument
            ),
            None,
        )
        spec = FOCUS_DIRECTIONS[direction]
        axis, step = spec
        perp = 1 - axis
        if from_focus is None:
            new_focus = min(
                active_focuses,
                key=lambda f: (
                    -step * (s := get_focus_spans(f, (True, True)))[axis][0],
                    s[perp][0],
                ),
            )
        else:
            new_focus = find_directional_focus(from_focus, spatial_focuses, spec)
        return True, (
            renpy.display.focus.change_focus(new_focus) if new_focus is not None else None
        )


    # --- Rollback and Roll-Forward State -------------------------------------


    def get_current_rollback_info() -> tuple[Any, Any]:
        """Return the current rollback log identifier and AST node name.

        Returns:
            tuple[Any, Any]: Pair of (rollback_identifier,
                ast_node_name), or (None, None) if the rollback log is
                unavailable.
        """
        log = getattr(renpy.game, "log", None)
        return (
            (log.current.identifier, log.current.context.current)
            if (log and log.current)
            else (None, None)
        )


    class ActiveRollForward(Null):
        """Underlay displayable handling directional focus and
        roll-forward replay.

        Registered in config.underlay so it receives per-interaction
        lifecycle hooks and input events across all interactions without
        altering screen layout. Tracks non-ending active-image clicks
        per AST node using plain non-rollback containers, prunes stale
        history in per_interact, and intercepts directional focus keys
        and roll-forward events in event.
        """

        def __init__(self) -> None:
            """Initialize the underlay displayable and non-rollback
            click tracking state.
            """
            super().__init__()
            self.forward_clicks: dict[Any, list[Any]] = builtins.dict()
            self.active_rb_id: Any = None
            self.active_node_name: Any = None
            self.interacted_in_node: bool = False
            self.current_node_clicks: list[Any] = builtins.list()

        def per_interact(self) -> None:
            """Synchronize replaced active images, track AST
            transitions, and prune roll-forward history.

            Called automatically by Ren'Py on every underlay displayable
            before each interaction begins. Ensures any replaced active
            images are re-wrapped and clears saved clicks for the
            previous node once it leaves the forward log without having
            been clicked or rolled forward.
            """
            sync_active_images()
            if renpy.game.interface.trans_pause or not renpy.game.context().rollback:
                return
            rb_id, node_name = get_current_rollback_info()
            if rb_id is None or rb_id == self.active_rb_id:
                return

            if (prev_node := self.active_node_name) is not None and not self.interacted_in_node:
                if all(f.name != prev_node for f in getattr(renpy.game.log, "forward", ())):
                    self.forward_clicks.pop(prev_node, None)

            self.active_rb_id, self.active_node_name = rb_id, node_name
            self.interacted_in_node = False
            self.current_node_clicks = builtins.list()

        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            """Process directional focus keys and roll-forward events.

            Args:
                ev (Any): Input event.
                x (int): Mouse X coordinate.
                y (int): Mouse Y coordinate.
                st (float): Shown time in seconds.

            Returns:
                Any: Interaction return value if triggered, otherwise
                    None.
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
            if not (
                actions := (self.forward_clicks.get(node_name) if node_name is not None else None)
            ):
                return None

            self.interacted_in_node = True
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


    def wrap_click(action: Any, widget: ActiveImage) -> Callable[[], Any] | None:
        """Wrap an ActiveImage button action to record clicks and store
        _return.

        Ren'Py's built-in roll-forward only saves the final return value
        that ended an interaction. Wrapping the button action during
        ActiveImage.event records each click on ActiveRollForward so
        side-effecting actions (like incrementing a counter during a
        dialogue line) replay on roll-forward, restores the widget's
        original action attributes before execution so saving inside an
        action remains picklable, and stores non-None results in
        store._return.

        Args:
            action (Any): Ren'Py Action or callable (or list of
                actions), or None.
            widget (ActiveImage): ActiveImage whose clicked/alternate
                should be restored before running.

        Returns:
            Callable[[], Any] | None: Wrapped action callable, or None.
        """
        if action is None:
            return None
        orig = (widget.clicked, widget.alternate)

        def wrapped() -> Any:
            widget.clicked, widget.alternate = orig
            _, node_name = get_current_rollback_info()
            rf = next((d for d in renpy.config.underlay if isinstance(d, ActiveRollForward)), None)
            if rf is not None and node_name is not None and renpy.game.context().rollback:
                rf.interacted_in_node = True
                rf.current_node_clicks.append(action)
                rf.forward_clicks[node_name] = builtins.list(rf.current_node_clicks)
            rv = renpy.display.behavior.run(action)
            if rv is not None and not isinstance(rv, (JumpException, CallException)):
                renpy.store._return = rv
                if (
                    renpy.exports.current_interact_type() in ("say", "pause")
                    and type(rv) is not bool
                ):
                    return True
            return rv

        return wrapped

define config.underlay += [active_images.ActiveRollForward()]
define config.statement_callbacks += [active_images.sync_active_images]
