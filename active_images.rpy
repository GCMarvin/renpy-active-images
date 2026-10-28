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
    from renpy.ast import Say
    from renpy.display.behavior import Button
    from renpy.display.core import Displayable, IgnoreEvent
    from renpy.display.focus import Focus
    from renpy.display.image import ImageReference
    from renpy.display.layout import Null
    from renpy.display.render import Render
    from renpy.display.scenelists import SceneListEntry, SceneLists
    from renpy.display.transform import Transform
    from renpy.game import CallException, JumpException
    from renpy.lexer import Lexer
    from renpy.python import NoRollback

    _constant = True


    ###########################################################################
    # `ActiveImage` displayable
    ###########################################################################


    class ActiveImage(Button):
        """A ``Button`` subclass designed for interactive scene-list
        sprites (``activate``).

        - Wraps the leaf image displayable directly inside any outer scene-list ``Transform``s,
            delegating ``get_placement()`` to its child and rendering at the child's exact
            surface size so ``image_placement`` / ``LayeredImage`` offsets are applied only once.
        - Sets `_store_transform_event = False` so scene-list ``"replace"`` events do not
            misfire ``Transform.last_child_transform_event`` on every dialogue line, while
            explicitly forwarding button ``hover``/`idle`/`insensitive` transform events and
            style prefixes upward to enclosing scene-list ``Transform``s.
        - Implements `_change_transform_child()` and hooks ``SceneLists.transform_state``
            so any normal ``show`` statement (with or without ``at``/ATL), character callback,
            or dialogue attribute change updates the image or outer transforms while
            keeping the sprite active.
        """

        _store_transform_event: bool = False
        last_button_event: str | None = None

        def __init__(
            self,
            child: Displayable | None = None,
            style: str = "default",
            focus_mask: Any = True,
            focus_name: str | None = None,
            **properties: Any,
        ) -> None:
            """Initialize an ``ActiveImage`` wrapper around ``child``.

            Args:
                child (Displayable | None, default=None): The leaf
                    displayable (typically an ``ImageReference``).
                style (str, default="default"): Button style name.
                    Defaults to ``"default"`` so button padding/margins
                    do not shift sprite placement.
                focus_mask (Any, default=True): Pixel hit-test mask
                    (``True``, ``None``, a displayable, or a callable).
                focus_name (str | None, default=None): Stable identifier
                    for focus persistence across interactions.
                **properties (Any): Additional ``Button`` keyword
                    arguments (``action``, ``hovered``, etc.).
            """
            super().__init__(None, style=style, focus_mask=focus_mask, **properties)
            if self.style is renpy.display.displayable.default_style:
                self.style = renpy.style.Style(style)
            self.last_button_event = None
            if focus_name is not None:
                self.focus_name = focus_name
            self._duplicatable = False
            if child is not None:
                self.set_child(child)

        def set_child(self, d: Displayable, duplicate: bool = True) -> None:
            """Set ``self.child`` to ``d``, optionally duplicating
            duplicatable displayables.

            Args:
                d (Displayable): The new child displayable.
                duplicate (bool, default=True): Whether to call
                    `_duplicate()` if ``d._duplicatable`` is ``True``.
                    Set to ``False`` when ``d`` is an already-copied
                    displayable.
            """
            child = renpy.easy.displayable(d)
            if duplicate and child._duplicatable:
                child = child._duplicate(None)
            self._duplicatable = False
            child._unique()
            self.children = [child]
            self.child = child
            self.offsets = []
            child.per_interact()
            renpy.display.render.invalidate(self)

        def get_placement(self) -> tuple:
            """Return placement tuple from ``self.child`` so
            ``ActiveImage`` is layout-transparent.

            Returns:
                tuple:
                    ``(xpos, ypos, xanchor, yanchor, xoffset, yoffset, subpixel)``.
            """
            if self.child is not None:
                return self.child.get_placement()
            return super().get_placement()

        def render(self, width: float, height: float, st: float, at: float) -> Render:
            """Render ``self.child`` and register an occlusion-aware
            focus region if active.

            Args:
                width (float): Available width.
                height (float): Available height.
                st (float): Shown time passed from the parent
                    displayable.
                at (float): Animation time passed from the parent
                    displayable.

            Returns:
                Render: The rendered ``Render`` surface for this sprite.
            """
            if self.style.time_policy:
                st, self.time_policy_data = self.style.time_policy(
                    st, self.time_policy_data, self.style
                )

            surf = renpy.display.render.render(self.child, width, height, st, at)
            sw, sh = surf.get_size()

            rv = Render(sw, sh)
            rv.blit(surf, (0, 0))
            self.offsets = [(0, 0)]
            self.window_size = (sw, sh)

            if not self.locked and self.clicked is not None:
                mask = self.style.focus_mask
                fmx = fmy = 0

                if mask is True:
                    mask = rv
                elif mask is not None and not callable(mask):
                    try:
                        mask = renpy.easy.displayable(mask)
                    except Exception:
                        raise Exception(
                            "Focus_mask must be None, True, a displayable, or a callable."
                        )

                if self.style.focus_rect is not None:
                    fx, fy, fw, fh = self.style.focus_rect
                    if isinstance(mask, Displayable):
                        fmx, fmy = fx, fy
                        mask = renpy.display.render.render(mask, fw, fh, st, at)
                else:
                    if isinstance(mask, Displayable):
                        mask = renpy.display.render.render(mask, sw, sh, st, at)
                    fx, fy, fw, fh = compute_active_focus_rect(surf, sw, sh)

                if mask is not None and mask is not False:
                    mask = make_occluded_focus_mask(rv, mask, fmx, fmy)

                rv.add_focus(self, None, fx, fy, fw, fh, fmx, fmy, mask)

            return rv

        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            """Delegate input events to ``Button.event`` with click
            recording wrappers applied.

            Args:
                ev (Any): The pygame input event.
                x (int): Local X coordinate.
                y (int): Local Y coordinate.
                st (float): Shown time in seconds.

            Returns:
                Any: The event result from ``Button.event``, or ``None``
                    if locked/unhandled.
            """
            if self.locked:
                return None

            orig_clicked, orig_alternate = self.clicked, self.alternate
            try:
                self.clicked = wrap_click(orig_clicked)
                self.alternate = wrap_click(orig_alternate)
                return super().event(ev, x, y, st)
            finally:
                self.clicked, self.alternate = orig_clicked, orig_alternate

        def set_transform_event(self, event: str) -> None:
            """Forward button state transform events upward to enclosing
            scene-list ``Transform``s.

            Args:
                event (str): The transform event name.
            """
            super().set_transform_event(event)
            if self.locked or event not in renpy.config.containers_pass_transform_events:
                return

            changed = event != self.last_button_event
            self.last_button_event = event

            for t in iter_enclosing_transforms(self):
                block = (t.get_block() or t.compile()) if hasattr(t, "get_block") else None
                has_pending_scene_event = (
                    t.transform_event is not None
                    and t.transform_event != getattr(t, "last_transform_event", None)
                    and block is not None
                    and block._handles_event(t.transform_event)
                )
                if not (has_pending_scene_event and event.endswith("idle")):
                    t.transform_event = event
                    if not changed and getattr(t, "atl_state", None) is not None:
                        t.last_transform_event = event
                    else:
                        renpy.display.render.redraw(t, 0)

        def set_style_prefix(self, prefix: str, root: bool) -> None:
            """Propagate the button's style prefix (``idle_``,
            ``hover_``, etc.) to enclosing ``Transform``s.

            Args:
                prefix (str): The style prefix string.
                root (bool): Whether ``self`` is the root of the prefix
                    change.
            """
            super().set_style_prefix(prefix, root)
            if self.locked:
                return
            for t in iter_enclosing_transforms(self):
                if getattr(t, "arguments", None) and any(t.arguments):
                    if t.style is renpy.display.displayable.default_style:
                        t.style = renpy.style.Style("default")
                    Displayable.set_style_prefix(t, prefix, False)
                    renpy.display.render.redraw(t, 0)

        def _handles_event(self, event: str) -> bool:
            """Report whether ``self.child`` handles the given transform
            event (``hide``/`replaced`).

            Args:
                event (str): The transform event name.

            Returns:
                bool: ``True`` if ``self.child`` handles ``event``,
                    ``False`` otherwise.
            """
            return self.child is not None and self.child._handles_event(event)

        def _hide(self, st: float, at: float, kind: str) -> Displayable | None:
            """Transition into a locked non-interactive state while
            ``self.child`` plays ``on hide:``.

            Args:
                st (float): Shown time in seconds.
                at (float): Animation time in seconds.
                kind (str): Hide kind (``"hide"`` or ``"replaced"``).

            Returns:
                Displayable | None: A locked copy of ``self`` wrapping
                    the hiding child, or ``None`` once complete.
            """
            if self.child is None or (new_child := self.child._hide(st, at, kind)) is None:
                return None
            rv = self._copy()
            rv.children = [new_child]
            rv.child = new_child
            rv.offsets = []
            rv.focusable = False
            rv.locked = True
            return rv

        def _in_current_store(self) -> Self:
            """Return a locked, non-focusable copy of ``self`` with
            children bound to the current store.

            Used by Ren'Py during ``with`` transitions and ``hide``
            animations so outgoing snapshots cannot be hovered or
            clicked.

            Returns:
                Self: A locked copy of ``self``.
            """
            rv = super()._in_current_store()
            rv.children = [i._in_current_store() for i in self.children]
            if rv.children:
                rv.child = rv.children[-1]
            rv.offsets = []
            return rv

        def _change_transform_child(self, child: Displayable) -> Displayable:
            """Update the leaf displayable when ``SceneLists.add``
            replaces a shown tag.

            Handles two cases:
            1. ``child`` is a new ``ActiveImage`` from ``activate``: transfer ``role``,
                ``last_button_event``, and focus replacement mapping onto ``child``.
            2. ``child`` is a leaf displayable from a normal ``show``, character callback,
                or dialogue attribute change: clone ``self`` around ``child`` while
                preserving button properties and focus state.

            Args:
                child (Displayable): The new displayable passed to
                    ``SceneLists.add``.

            Returns:
                Displayable: The updated ``ActiveImage`` preserving
                    button and focus continuity.
            """
            if isinstance(child, ActiveImage):
                return transfer_active_state(self, child)

            rv = self._copy()
            rv.set_child(child, duplicate=False)
            return transfer_active_state(self, rv, copy_role=False)


    ###########################################################################
    # Creator-defined statements (`activate`, `deactivate`, and `call active`)
    ###########################################################################

    BUTTON_CLAUSES: tuple[str, ...] = (
        "action",
        "hovered",
        "unhovered",
        "alternate",
        "sensitive",
        "selected",
        "focus_mask",
    )


    def parse_activate_or_deactivate(
        l: Lexer, allow_button_clauses: bool = True
    ) -> dict[str, Any]:
        """Parse an ``activate`` or ``deactivate`` statement.

        Temporarily extends ``IMAGE_KEYWORDS`` with ``BUTTON_CLAUSES``
        so ``parse_image_specifier`` stops before button properties
        (``action``, ``hovered``, etc.).

        Args:
            l (Lexer): The Ren'Py script lexer.
            allow_button_clauses (bool, default=True): ``True`` for
                ``activate``, ``False`` for ``deactivate``.

        Returns:
            dict[str, Any]: A dict containing ``"imspec"``,
                ``"with_expr"``, and ``"button_clauses"``.
        """
        stmt = "activate" if allow_button_clauses else "deactivate"
        old_keywords = renpy.lexer.IMAGE_KEYWORDS
        try:
            if allow_button_clauses:
                renpy.lexer.IMAGE_KEYWORDS = tuple(old_keywords) + BUTTON_CLAUSES
            imspec = renpy.parser.parse_image_specifier(l)
        finally:
            renpy.lexer.IMAGE_KEYWORDS = old_keywords

        with_expr = None
        button_clauses = {}

        while True:
            if l.keyword("with"):
                if with_expr is not None:
                    l.error(f"{stmt} statement given multiple with clauses.")
                with_expr = l.require(l.simple_expression)
            elif allow_button_clauses and (
                kw := next((k for k in BUTTON_CLAUSES if l.keyword(k)), None)
            ):
                if kw in button_clauses:
                    l.error(f"{stmt} statement given multiple {kw} clauses.")
                button_clauses[kw] = l.require(l.simple_expression)
            else:
                break

        if l.match(":"):
            l.expect_block(f"{stmt} statement")
        else:
            l.expect_noblock(f"{stmt} statement")

        l.expect_eol()
        return {"imspec": imspec, "with_expr": with_expr, "button_clauses": button_clauses}


    def execute_activate(p: dict[str, Any], atl: Any = None) -> None:
        """Execute the ``activate`` statement.

        Wraps the target leaf image in an ``ActiveImage`` button
        (inheriting unspecified button clauses if the tag is already
        active) and delegates showing and any ``at``/ATL transform
        wrapping to ``renpy.exports.show``.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                ``parse_activate_or_deactivate``.
            atl (Any, default=None): Optional compiled ATL transform
                block attached to the statement.
        """
        name, expression, what, tag, key, at_vals, resolved_layer, zorder, behind = resolve_imspec(
            p["imspec"]
        )
        with_expr = p["with_expr"]

        if with_expr is not None:
            renpy.exports.with_statement(None)

        sle = get_sle(resolved_layer, key)
        existing_active = find_active_image(sle.displayable) if sle is not None else None
        new_name, leaf_img = resolve_target_image(name, expression, what, key, resolved_layer, sle)

        btn_kwargs = {kw: renpy.python.py_eval(expr) for kw, expr in p["button_clauses"].items()}
        if existing_active is not None:
            for kw in BUTTON_CLAUSES:
                if kw not in btn_kwargs:
                    btn_kwargs[kw] = (
                        existing_active.style.focus_mask
                        if kw == "focus_mask"
                        else getattr(existing_active, kw)
                    )
        else:
            btn_kwargs.setdefault("action", renpy.store.NullAction())
            btn_kwargs.setdefault("focus_mask", True)

        active_img = ActiveImage(
            child=leaf_img, focus_name=f"active_image:{resolved_layer}:{key}", **btn_kwargs
        )
        if existing_active is not None:
            transfer_active_state(existing_active, active_img)

        renpy.exports.show(
            new_name,
            at_list=at_vals,
            layer=resolved_layer,
            what=active_img,
            zorder=zorder,
            tag=tag,
            behind=behind,
            atl=atl,
        )

        if (
            isinstance(default_transform := renpy.config.default_transform, Transform)
            and (new_sle := get_sle(resolved_layer, key)) is not None
            and isinstance(new_sle.displayable, Transform)
        ):
            state = new_sle.displayable.state
            for pos_attr, inh_attr, val in zip(
                ("xpos", "ypos", "xanchor", "yanchor"),
                ("inherited_xpos", "inherited_ypos", "inherited_xanchor", "inherited_yanchor"),
                default_transform.get_placement()[:4],
            ):
                if (
                    getattr(state, pos_attr, None) is None
                    and getattr(state, inh_attr, None) is None
                    and val is not None
                ):
                    setattr(state, inh_attr, val)

        if with_expr is not None:
            renpy.exports.with_statement(renpy.python.py_eval(with_expr))


    def execute_deactivate(p: dict[str, Any], atl: Any = None) -> None:
        """Execute the ``deactivate`` statement.

        Removes the ``ActiveImage`` wrapper from the target sprite while
        preserving its outer ``Transform`` chain, transitions enclosing
        hover transforms back to ``"idle"``, and applies any optional
        ``show`` clauses or ATL block. No-ops if the target tag is not
        currently showing.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                ``parse_activate_or_deactivate``.
            atl (Any, default=None): Optional compiled ATL transform
                block attached to the statement.
        """
        name, expression, what, tag, key, at_vals, resolved_layer, zorder, behind = resolve_imspec(
            p["imspec"]
        )
        with_expr = p["with_expr"]

        if not renpy.exports.showing(key, layer=resolved_layer):
            return

        if with_expr is not None:
            renpy.exports.with_statement(None)

        sle = get_sle(resolved_layer, key)
        existing_active = find_active_image(sle.displayable) if sle is not None else None
        new_name, leaf_img = resolve_target_image(name, expression, what, key, resolved_layer, sle)

        if sle is not None and existing_active is not None:
            sle_copy = sle.copy()
            sle_copy.displayable = unwrap_active_image(sle.displayable)
            layer_list = renpy.game.context().scene_lists.layers[resolved_layer]
            layer_list[layer_list.index(sle)] = sle_copy

        renpy.exports.show(
            new_name,
            at_list=at_vals,
            layer=resolved_layer,
            what=leaf_img,
            zorder=zorder,
            tag=tag,
            behind=behind,
            atl=atl,
        )

        if existing_active is not None and (new_sle := get_sle(resolved_layer, key)) is not None:
            d = new_sle.displayable
            while isinstance(d, Transform):
                block = (d.get_block() or d.compile()) if hasattr(d, "get_block") else None
                if block is None or not block._handles_event(d.transform_event):
                    d.transform_event = "idle"
                if d.style is not renpy.display.displayable.default_style:
                    Displayable.set_style_prefix(d, "idle_", False)
                d = d.child

        if with_expr is not None:
            renpy.exports.with_statement(renpy.python.py_eval(with_expr))


    def predict_activate_or_deactivate(p: dict[str, Any]) -> list[Any]:
        """Predict displayables referenced by ``activate`` or
        ``deactivate``.

        Args:
            p (dict[str, Any]): Parsed statement dictionary.

        Returns:
            list[Any]: An empty list (prediction is registered via
                ``predict_imspec``).
        """
        renpy.ast.predict_imspec(p["imspec"])
        return []


    def lint_activate_or_deactivate(p: dict[str, Any], is_deactivate: bool = False) -> None:
        """Validate layers, images, transforms, and button expressions
        during lint.

        Args:
            p (dict[str, Any]): Parsed statement dictionary.
            is_deactivate (bool, default=False): ``True`` when linting
                ``deactivate``, ``False`` when linting ``activate``.
        """
        name, expression, tag, at_list, layer, zorder, behind = p["imspec"]
        stmt = "deactivate" if is_deactivate else "activate"
        key = tag or (name[0] if name else None)

        resolved_layer = renpy.exports.default_layer(layer, key, expression is not None)
        if resolved_layer not in renpy.display.scenelists.layers:
            renpy.lint.report("Uses layer '%s', which is not defined.", resolved_layer)

        if expression is not None:
            if key is not None:
                renpy.lint.image_prefixes[key] = True
            renpy.lint.try_eval(f"the expression of a {stmt} statement", expression)
        elif is_deactivate:
            if key not in renpy.lint.image_prefixes:
                renpy.lint.report(
                    "The image tag '%s' is not the prefix of a declared image, nor was it used in"
                    " a show statement before this deactivate statement.",
                    key,
                )
        else:
            renpy.lint.image_exists(name, expression, tag, precise=False)

        if zorder is not None:
            renpy.lint.try_eval(f"the zorder of a {stmt} statement", zorder)

        for expr in at_list:
            renpy.lint.try_eval(
                f"the at list of a {stmt} statement",
                expr,
                "Perhaps you forgot to define or misspelled a transform.",
            )

        for kw, expr in p.get("button_clauses", {}).items():
            renpy.lint.try_eval(f"the {kw} clause of a {stmt} statement", expr)

        if p.get("with_expr") is not None:
            renpy.lint.try_eval(f"the with clause of a {stmt} statement", p["with_expr"])


    def parse_call_active(l: Lexer) -> dict[str, Any]:
        """Parse a ``call active`` statement.

        Supports an optional timeout expression (``call active 5.0``)
        and an optional single-line ``say`` subblock whose dialogue is
        displayed without blocking before the pause interaction begins
        (``subparse.block[0].interact = False``).

        Args:
            l (Lexer): The Ren'Py script lexer.

        Returns:
            dict[str, Any]: A dict with ``"delay"`` (expression string
                or ``None``) and ``"subparse"`` (subblock or ``None``).
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
        """Prepare rollback and skipping state before ``call active``.

        Defers `_begin_rollback` until ``post_execute_call_active`` when
        a ``say`` subblock is present so rolling back returns to the
        ``call active`` node as a single unit.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                ``parse_call_active``.
        """
        renpy.exports.choice_for_skipping()
        renpy.config.skipping = None
        renpy.store._begin_rollback = False

        if p["subparse"] is None:
            renpy.store._window_auto = True


    def post_execute_call_active(p: dict[str, Any]) -> None:
        """Run the interactive pause for ``call active`` after any
        ``say`` subblock.

        Waits for an active sprite action (or timeout ``delay``),
        checkpoints rollback, and stores the result in
        ``store._return``.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                ``parse_call_active``.
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
            rv = renpy.ui.interact(
                mouse="pause", type="pause", roll_forward=roll_forward, pause=delay
            )
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
        """Validate the optional delay expression of ``call active``.

        Args:
            p (dict[str, Any]): Parsed statement dictionary from
                ``parse_call_active``.
        """
        if p["delay"] is not None:
            renpy.lint.try_eval("the delay of a call active statement", p["delay"])


    renpy.register_statement(
        "activate",
        parse=lambda l: parse_activate_or_deactivate(l, allow_button_clauses=True),
        execute=execute_activate,
        predict=predict_activate_or_deactivate,
        lint=lambda p: lint_activate_or_deactivate(p, is_deactivate=False),
        block="atl-possible",
        warp=lambda p: True,
    )

    renpy.register_statement(
        "deactivate",
        parse=lambda l: parse_activate_or_deactivate(l, allow_button_clauses=False),
        execute=execute_deactivate,
        predict=predict_activate_or_deactivate,
        lint=lambda p: lint_activate_or_deactivate(p, is_deactivate=True),
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


    ###########################################################################
    # Scene-list, image resolution, and transform state helpers
    ###########################################################################


    def get_sle(layer: str, key: str) -> SceneListEntry | None:
        """Find the most recent ``SceneListEntry`` for ``key`` on
        ``layer``.

        Args:
            layer (str): The target layer name.
            key (str): The image tag.

        Returns:
            SceneListEntry | None: The matching ``SceneListEntry``, or
                ``None`` if ``key`` is not showing on ``layer``.
        """
        return next(
            (
                sle
                for sle in reversed(renpy.game.context().scene_lists.layers.get(layer, ()))
                if sle.tag == key
            ),
            None,
        )


    def find_active_image(d: Displayable | None) -> ActiveImage | None:
        """Find the ``ActiveImage`` in the ``d -> d.child`` transform
        chain, if any.

        Args:
            d (Displayable | None): Root displayable of a
                ``SceneListEntry`` (or ``None``).

        Returns:
            ActiveImage | None: The matching ``ActiveImage``, or
                ``None`` if ``d`` is not active.
        """
        while d is not None:
            if isinstance(d, ActiveImage):
                return d
            d = getattr(d, "child", None)
        return None


    def iter_enclosing_transforms(target: Displayable) -> list[Transform]:
        """Return all scene-list ``Transform`` wrappers enclosing
        ``target``.

        Walks the current context's ``SceneLists`` layers to find any
        entry whose transform chain contains ``target``, collecting all
        ``Transform`` instances above ``target``.

        Args:
            target (Displayable): The ``Displayable`` (typically an
                ``ActiveImage``) to search for.

        Returns:
            list[Transform]: A list of ``Transform`` instances enclosing
                ``target``.
        """
        result = []
        ctx = renpy.game.context()
        if ctx is None or getattr(ctx, "scene_lists", None) is None:
            return result
        for layer_list in ctx.scene_lists.layers.values():
            for sle in layer_list:
                transforms = []
                d = sle.displayable
                while d is not None:
                    if d is target:
                        result.extend(transforms)
                        break
                    if isinstance(d, Transform):
                        transforms.append(d)
                    d = getattr(d, "child", None)
        return result


    def transfer_active_state(
        old_widget: ActiveImage, new_widget: ActiveImage, copy_role: bool = True
    ) -> ActiveImage:
        """Transfer button state and focus replacement mapping from
        ``old_widget`` to ``new_widget``.

        When character callbacks, dialogue attribute changes, or
        ``activate``/``show`` statements replace an ``ActiveImage``
        mid-interaction, transferring ``role``, ``last_button_event``,
        and ``replaced_by`` (including transitive chains) preserves
        hover and focus state across interaction restarts.

        Args:
            old_widget (ActiveImage): The previous ``ActiveImage``
                instance being replaced.
            new_widget (ActiveImage): The new ``ActiveImage`` instance
                taking its place.
            copy_role (bool, default=True): Whether to copy
                ``old_widget.role`` onto ``new_widget``.

        Returns:
            ActiveImage: ``new_widget`` with state transferred.
        """
        if copy_role:
            new_widget.role = old_widget.role
        new_widget.last_button_event = old_widget.last_button_event
        replaced_by = renpy.display.focus.replaced_by
        for k, v in list(replaced_by.items()):
            if v is old_widget:
                replaced_by[k] = new_widget
        replaced_by[id(old_widget)] = new_widget
        return new_widget


    def inject_active_child(d: Displayable, old_active: ActiveImage) -> Displayable:
        """Wrap the leaf displayable of ``d`` in a copy of
        ``old_active``.

        Used by ``preserve_active_transform_state`` when a normal
        ``show`` statement with ``at`` or ATL blocks replaces the
        transform chain of an active sprite.

        Args:
            d (Displayable): The new displayable or ``Transform`` chain
                being shown.
            old_active (ActiveImage): The currently active
                ``ActiveImage`` to preserve.

        Returns:
            Displayable: ``d`` with its leaf displayable wrapped in a
                copy of ``old_active``.
        """
        if isinstance(d, Transform) and d.child is not None:
            d.set_child(inject_active_child(d.child, old_active), duplicate=False)
            return d
        return old_active._change_transform_child(d)


    def unwrap_active_image(d: Displayable) -> Displayable:
        """Return a copy of ``d``'s transform chain with any
        ``ActiveImage`` wrapper removed.

        Clones enclosing ``Transform`` instances rather than mutating
        them in-place so displayables stored in earlier rollback
        checkpoints remain intact.

        Args:
            d (Displayable): Root displayable of a ``SceneListEntry``.

        Returns:
            Displayable: The displayable or ``Transform`` chain with
                ``ActiveImage`` replaced by its child.
        """
        if isinstance(d, ActiveImage):
            return unwrap_active_image(d.child) if d.child is not None else Null()
        if isinstance(d, Transform) and d.child is not None:
            d.get_placement()
            rv = d.copy()
            rv.active = d.active
            rv.set_child(unwrap_active_image(d.child), duplicate=False)
            rv._unique()
            return rv
        return d


    def resolve_imspec(
        imspec: tuple,
    ) -> tuple[
        tuple[str, ...],
        str | None,
        Displayable | None,
        str | None,
        str,
        list[Any],
        str,
        int | None,
        list[str],
    ]:
        """Evaluate runtime expressions in an ``imspec`` tuple and
        resolve the target tag and layer.

        Handles anonymous ``activate expression`` statements by
        generating a unique `_show_expression_N` tag when no ``as`` tag
        is given.

        Args:
            imspec (tuple): The 7-tuple returned by
                ``parse_image_specifier``.

        Returns:
            tuple[tuple[str, ...], str | None, Displayable | None, str | None, str, list[Any], str, int | None, list[str]]:
                ``(name, expression, what, tag, key, at_vals, resolved_layer, zorder, behind)``.
        """
        name, expression, tag, at_list, layer, zorder, behind = imspec
        zorder = renpy.python.py_eval(zorder) if zorder is not None else None
        behind = list(behind) if behind is not None else []
        at_vals = [renpy.python.py_eval(i) for i in at_list]

        if expression is not None:
            what = renpy.python.py_eval(expression)
            if not renpy.config.old_show_expression:
                if isinstance(what, str):
                    name = tuple(what.split())
                else:
                    if tag is None:
                        i = 0
                        while renpy.exports.showing(f"_show_expression_{i}", layer=layer):
                            i += 1
                        tag = f"_show_expression_{i}"
                    name = (tag,)
            what = renpy.easy.displayable(what)
        else:
            what = None

        key = tag or name[0]
        resolved_layer = renpy.exports.default_layer(
            layer, key, (expression is not None) and (tag is None)
        )
        return (name, expression, what, tag, key, at_vals, resolved_layer, zorder, behind)


    def resolve_target_image(
        name: tuple[str, ...],
        expression: str | None,
        what: Displayable | None,
        key: str,
        resolved_layer: str,
        sle: SceneListEntry | None = None,
    ) -> tuple[tuple[str, ...], Displayable]:
        """Resolve the image name tuple and leaf displayable for
        ``activate`` or ``deactivate``.

        When updating or deactivating an already-shown tag without
        ``expression``, inspects the existing leaf displayable in
        ``sle`` so custom ``as`` tags and ``show expression`` /
        ``activate expression`` displayables resolve to their actual
        underlying image or displayable.

        Args:
            name (tuple[str, ...]): Parsed image name tuple.
            expression (str | None): Raw expression string if
                ``expression`` was used, else ``None``.
            what (Displayable | None): Evaluated expression displayable
                if ``expression`` was used, else ``None``.
            key (str): Effective image tag.
            resolved_layer (str): Target layer name.
            sle (SceneListEntry | None, default=None): Existing
                ``SceneListEntry`` for ``(resolved_layer, key)``, if
                any.

        Returns:
            tuple[tuple[str, ...], Displayable]:
                ``(new_name, leaf_displayable)``.
        """
        if expression is not None:
            return (key,) + name[1:], what

        if what is None and sle is not None:
            d = sle.displayable
            while isinstance(d, (Transform, ActiveImage)) and d.child is not None:
                d = d.child

            # Reuse raw displayable if originally shown via `show/activate expression ... as tag`.
            if d is not None and not isinstance(d, ImageReference):
                if name == (key,):
                    return sle.name or name, d
            # Map custom `as` tag back to the base image tag (e.g. `show/activate eileen as hero`).
            elif (
                isinstance(d, ImageReference)
                and isinstance(d.name, tuple)
                and d.name
                and name[0] == key
                and d.name[0] != key
                and renpy.display.image.images.get(name) is None
                and not renpy.display.image.image_attributes.get(key)
            ):
                name = (d.name[0],) + name[1:]

        new_name, full_name = renpy.exports.displayexports._find_image(
            resolved_layer, key, name, None
        )
        return (key,) + new_name[1:], ImageReference(full_name, style="image_placement")


    orig_transform_state = getattr(
        SceneLists.transform_state, "orig_transform_state", SceneLists.transform_state
    )


    def preserve_active_transform_state(
        self: SceneLists,
        old_thing: Displayable | None,
        new_thing: Displayable,
        execution: bool = False,
    ) -> Displayable:
        """Wrap ``SceneLists.transform_state`` so normal ``show``
        statements preserve ``ActiveImage``.

        When an active sprite is updated by a normal ``show`` statement
        with ``at`` transforms or an ATL block (``execution=False``),
        injects a copy of the existing ``ActiveImage`` around the new
        leaf displayable before transferring outer transform state.

        Args:
            self (SceneLists): The active ``SceneLists`` instance.
            old_thing (Displayable | None): The previous root
                displayable on the scene list, or ``None``.
            new_thing (Displayable): The new root displayable being
                shown.
            execution (bool, default=False): Whether ATL execution state
                is also being transferred.

        Returns:
            Displayable: ``new_thing`` with transform state (and
                ``ActiveImage`` wrapper, if active) preserved.
        """
        if old_thing is not None:
            old_thing.get_placement()
        if not execution and (old_active := find_active_image(old_thing)) is not None:
            new_active = find_active_image(new_thing)
            if new_active is None:
                new_thing = inject_active_child(new_thing, old_active)
            elif new_active is not old_active:
                transfer_active_state(old_active, new_active)
        return orig_transform_state(self, old_thing, new_thing, execution=execution)


    preserve_active_transform_state.orig_transform_state = orig_transform_state
    SceneLists.transform_state = preserve_active_transform_state


    ###########################################################################
    # Spatial focus bounds, occlusion, and directional navigation
    ###########################################################################

    # Direction specifications for spatial keyboard/controller navigation:
    # `(axis, step)` where `axis` is the movement dimension (`0` or `1`) and `step`
    # is `+1` (increasing screen coordinates) or `-1` (decreasing screen coordinates).
    FOCUS_DIRECTIONS: dict[str, tuple[int, int]] = {
        "focus_right": (0, 1),
        "focus_left": (0, -1),
        "focus_down": (1, 1),
        "focus_up": (1, -1),
    }


    def get_render_content_bounds(r: Render) -> tuple[float, float, float, float] | None:
        """Compute the tight ``(x0, y0, x1, y1)`` bounding box of
        visible leaf surfaces in ``r``.

        ``LayeredImage`` sprites frequently pad their canvas to
        full-screen dimensions with transparent space. Extracting the
        actual drawn sub-surface bounds prevents full-screen focus
        rectangles that break directional navigation.

        Args:
            r (Render): The ``Render`` tree to inspect.

        Returns:
            tuple[float, float, float, float] | None:
                ``(x0, y0, x1, y1)`` in ``r``'s local coordinates, or
                ``None`` if ``r`` has no visible leaves.
        """
        bx0 = by0 = float("inf")
        bx1 = by1 = float("-inf")
        rev = getattr(r, "reverse", None)
        # Check if `r.reverse` differs from the 2D identity matrix (1, 0, 0, 1).
        has_matrix = rev is not None and (rev.xdx, rev.xdy, rev.ydx, rev.ydy) != (
            1.0,
            0.0,
            0.0,
            1.0,
        )

        for child, xo, yo, focus, main in r.children:
            if isinstance(child, Render):
                if (cb := get_render_content_bounds(child)) is None:
                    continue
                cx0, cy0, cx1, cy1 = cb
            elif hasattr(child, "get_bounding_rect"):
                rx, ry, rw, rh = child.get_bounding_rect()
                if rw <= 0 or rh <= 0:
                    continue
                cx0, cy0, cx1, cy1 = (float(rx), float(ry), float(rx + rw), float(ry + rh))
            elif hasattr(child, "get_size"):
                cw, ch = child.get_size()
                if cw <= 0 or ch <= 0:
                    continue
                cx0, cy0, cx1, cy1 = 0.0, 0.0, float(cw), float(ch)
            else:
                continue

            # Map child corners through `r.reverse` when `r` applies zoom/flip/rotation.
            if has_matrix:
                xs, ys = zip(*(rev.transform(x, y) for x in (cx0, cx1) for y in (cy0, cy1)))
                tx0, ty0, tx1, ty1 = (min(xs) + xo, min(ys) + yo, max(xs) + xo, max(ys) + yo)
            else:
                tx0, ty0, tx1, ty1 = cx0 + xo, cy0 + yo, cx1 + xo, cy1 + yo

            bx0, by0 = min(bx0, tx0), min(by0, ty0)
            bx1, by1 = max(bx1, tx1), max(by1, ty1)

        if getattr(r, "xclipping", False):
            bx0, bx1 = max(0.0, bx0), min(float(r.width), bx1)
        if getattr(r, "yclipping", False):
            by0, by1 = max(0.0, by0), min(float(r.height), by1)

        return (bx0, by0, bx1, by1) if (bx0 < bx1 and by0 < by1) else None


    def compute_active_focus_rect(
        surf: Render, sw: float, sh: float
    ) -> tuple[float, float, float, float]:
        """Compute the tight ``(fx, fy, fw, fh)`` visible bounding box
        of ``surf``.

        Args:
            surf (Render): The rendered child surface.
            sw (float): Width of ``surf``.
            sh (float): Height of ``surf``.

        Returns:
            tuple[float, float, float, float]: ``(fx, fy, fw, fh)`` in
                local coordinates.
        """
        if (bounds := get_render_content_bounds(surf)) is not None:
            bx0, by0 = max(0.0, bounds[0]), max(0.0, bounds[1])
            bx1, by1 = min(float(sw), bounds[2]), min(float(sh), bounds[3])
        else:
            bx0, by0, bx1, by1 = 0.0, 0.0, float(sw), float(sh)

        return (bx0, by0, max(1.0, bx1 - bx0), max(1.0, by1 - by0))


    def find_parent_render(curr: Render) -> tuple[Render | None, int, float, float]:
        """Locate ``curr``'s active parent in the current screen render
        tree.

        Checks ``focus=True`` on child entries so internal off-tree
        caches (such as ``Transform.child_cache``, which blits with
        ``focus=False``) are skipped.

        Args:
            curr (Render): The ``Render`` whose parent is sought.

        Returns:
            tuple[Render | None, int, float, float]:
                ``(parent, child_index, x_offset, y_offset)``, or
                ``(None, -1, 0, 0)`` at the root.
        """
        for p in curr.parents:
            for idx, (child, xo, yo, focus, main) in enumerate(p.children):
                if child is curr and focus:
                    return p, idx, xo, yo
        return None, -1, 0, 0


    def is_pixel_occluded(rv: Render, cx: float, cy: float) -> bool:
        """Check if local pixel ``(cx, cy)`` on ``rv`` is obscured by
        any foreground ``Render``.

        Walks up ``rv``'s ancestor chain to the screen root. At each
        ancestor, checks whether ``(cx, cy)`` falls outside clipping
        bounds or hits an opaque pixel on any later sibling ``Render``
        (higher ``zorder`` on the same layer or higher layers such as
        ``screens``).

        Args:
            rv (Render): The ``ActiveImage``'s ``Render``.
            cx (float): Local X coordinate on ``rv``.
            cy (float): Local Y coordinate on ``rv``.

        Returns:
            bool: ``True`` if ``(cx, cy)`` is clipped or covered by a
                foreground pixel, ``False`` otherwise.
        """
        curr = rv
        while True:
            parent, idx, xo, yo = find_parent_render(curr)
            if parent is None:
                return False

            # Transform child coordinates into `parent`'s coordinate space.
            if parent.reverse is not None:
                cx, cy = parent.reverse.transform(cx, cy)
            px = cx + xo
            py = cy + yo

            if (parent.xclipping and not (0 <= px < parent.width)) or (
                parent.yclipping and not (0 <= py < parent.height)
            ):
                return True

            # Check all siblings drawn after `curr` (in front of `curr`).
            for sib, sxo, syo, sfocus, smain in parent.children[idx + 1 :]:
                if not isinstance(sib, Render):
                    continue
                sx, sy = px - sxo, py - syo
                if parent.forward is not None:
                    sx, sy = parent.forward.transform(sx, sy)
                if sib.is_pixel_opaque(round(sx), round(sy)):
                    return True

            curr = parent
            cx, cy = px, py


    def make_occluded_focus_mask(
        rv: Render, base_mask: Any, fmx: int, fmy: int
    ) -> Callable[[int | float, int | float], bool]:
        """Wrap ``base_mask`` into a callable focus mask that rejects
        occluded pixels.

        Ren'Py evaluates callable focus masks lazily during
        ``find_focus()`` at mouse coordinates ``(mx, my)`` relative to
        ``(fmx, fmy)``, after the full scene render tree
        (``parents``/`children`) has been constructed.

        Args:
            rv (Render): The ``ActiveImage``'s ``Render``.
            base_mask (Any): The unoccluded mask (``Render`` or
                callable).
            fmx (int): X offset of ``base_mask`` within ``rv``.
            fmy (int): Y offset of ``base_mask`` within ``rv``.

        Returns:
            Callable[[int | float, int | float], bool]: A callable
                ``(mx, my) -> bool`` suitable for ``Render.add_focus``.
        """

        def focus_mask(mx: int | float, my: int | float) -> bool:
            if isinstance(base_mask, Render):
                if not base_mask.is_pixel_opaque(int(mx), int(my)):
                    return False
            elif callable(base_mask):
                if not base_mask(mx, my):
                    return False
            return not is_pixel_occluded(rv, mx + fmx, my + fmy)

        return focus_mask


    def get_focus_spans(
        f: Focus, collapse: tuple[bool, bool] = (False, False)
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Compute the screen-clamped ``((p0, p1), (q0, q1))``
        coordinate spans of ``f`` across both dimensions.

        Clamps ``ActiveImage`` bounds to the visible screen dimensions.
        When ``collapse[axis]`` is ``True``, collapses that dimension of
        an ``ActiveImage`` to its midpoint (``p0 = p1 = center``) so
        overlapping sprites are compared by their center positions
        rather than their outer edges.

        Args:
            f (Focus): The ``Focus`` entry.
            collapse (tuple[bool, bool], default=(False, False)):
                Whether to collapse each dimension of an ``ActiveImage``
                to its midpoint.

        Returns:
            tuple[tuple[float, float], tuple[float, float]]:
                ``((d0_min, d0_max), (d1_min, d1_max))`` in screen
                coordinates.
        """
        rect = f.inset_rect()
        origins, sizes = rect[:2], rect[2:]
        if not isinstance(f.widget, ActiveImage):
            return (
                (float(origins[0]), float(origins[0] + sizes[0])),
                (float(origins[1]), float(origins[1] + sizes[1])),
            )

        limits = (renpy.config.screen_width, renpy.config.screen_height)
        spans = []
        for origin, size, limit, col in zip(origins, sizes, limits, collapse):
            p0, p1 = float(origin), float(origin + size)
            c0, c1 = max(0.0, p0), min(float(limit), p1)
            if c0 < c1:
                p0, p1 = c0, c1
            if col:
                p0 = p1 = (p0 + p1) * 0.5
            spans.append((p0, p1))
        return (spans[0], spans[1])


    def find_directional_focus(
        from_focus: Focus, candidates: list[Focus], spec: tuple[int, int]
    ) -> Focus | None:
        """Find the nearest candidate ``Focus`` from ``from_focus`` in
        the direction of ``spec``.

        - For sprite-to-sprite transitions, collapses the movement dimension to each
            sprite's center while keeping full perpendicular spans so arbitrarily
            overlapping sprites are traversed in strict center order.
        - For cross-group transitions (between ``ActiveImage`` sprites and UI widgets),
            collapses the sprite to its center point across both dimensions and requires
            the target to lie within the 90-degree directional cone
            (``perp_gap <= main_gap``).

        Args:
            from_focus (Focus): The currently focused entry.
            candidates (list[Focus]): Eligible target ``Focus`` entries.
            spec (tuple[int, int]): ``(axis, step)`` tuple from
                ``FOCUS_DIRECTIONS``.

        Returns:
            Focus | None: The best matching ``Focus`` entry, or ``None``
                if no candidate lies in that direction.
        """
        axis, step = spec
        perp = 1 - axis
        from_is_active = isinstance(from_focus.widget, ActiveImage)
        penalty = renpy.config.focus_crossrange_penalty

        best = None
        best_score = (float("inf"), float("inf"))
        for f in candidates:
            if f.widget is from_focus.widget and f.arg == from_focus.arg:
                continue

            to_is_active = isinstance(f.widget, ActiveImage)
            cross_group = from_is_active != to_is_active
            collapse = (axis == 0 or cross_group, axis == 1 or cross_group)
            o_spans = get_focus_spans(from_focus, collapse)
            n_spans = get_focus_spans(f, collapse)

            om0, om1 = o_spans[axis]
            op0, op1 = o_spans[perp]
            nm0, nm1 = n_spans[axis]
            np0, np1 = n_spans[perp]

            omc, opc = (om0 + om1) * 0.5, (op0 + op1) * 0.5
            nmc, npc = (nm0 + nm1) * 0.5, (np0 + np1) * 0.5

            # If intervals on the movement axis still overlap across groups (e.g. a
            # large UI panel covering a sprite's center), compare their centers directly.
            if cross_group and om0 < nm1 and nm0 < om1:
                om0 = om1 = omc
                nm0 = nm1 = nmc

            if (nmc - omc) * step <= 0:
                continue
            if (step > 0 and om1 > nm0) or (step < 0 and nm1 > om0):
                continue

            main_gap = (nm0 - om1) if step > 0 else (om0 - nm1)
            perp_gap = max(0.0, np0 - op1, op0 - np1)
            perp_center_diff = abs(npc - opc)

            if cross_group and perp_gap > main_gap:
                continue
            if from_is_active and to_is_active and perp_center_diff > main_gap:
                continue

            score = (main_gap**2 + (perp_gap * penalty) ** 2, perp_center_diff)
            if score < best_score:
                best, best_score = f, score
        return best


    def handle_active_spatial_focus(ev: Any) -> tuple[bool, Any]:
        """Handle keyboard/controller directional focus
        (``focus_left``/`right`/`up`/`down`).

        Standard Ren'Py directional focus can fail on scene-list sprites
        when focus rects overlap or when no widget is currently focused
        (defaulting to UI overlay buttons instead of sprites). When
        ``ActiveImage``s are present, this selects the best initial or
        directional target across sprites and UI screens.

        Args:
            ev (Any): The pygame input event.

        Returns:
            tuple[bool, Any]: ``(handled, result)`` where ``handled`` is
                ``True`` if ``ev`` was consumed as a directional focus
                event, and ``result`` is the return value of
                ``change_focus()``.
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
            # When nothing is focused yet, pick the extreme ActiveImage for the pressed direction.
            def initial_sort_key(f: Focus) -> tuple[float, float]:
                spans = get_focus_spans(f, (True, True))
                return (-step * spans[axis][0], spans[perp][0])

            new_focus = min(active_focuses, key=initial_sort_key)
        else:
            new_focus = find_directional_focus(from_focus, spatial_focuses, spec)

        return True, (
            renpy.display.focus.change_focus(new_focus) if new_focus is not None else None
        )


    ###########################################################################
    # Rollback and rollforward state
    ###########################################################################


    class RollForwardState(NoRollback):
        """Non-rollback state for recording ``ActiveImage`` clicks and
        replaying them on rollforward.

        Inherits from ``NoRollback`` so clicks recorded before a
        rollback survive the state restoration and can be re-executed
        when the player rolls forward.
        """

        forward_clicks: dict[Any, list[Any]] = python_dict()
        active_rb_id: Any = None
        active_node_name: Any = None
        clicked_in_current_node: bool = False
        rolled_forward_in_current_node: bool = False
        current_node_clicks: list[Any] = python_list()
        in_call_active: bool = False


    class ActiveRollForward(Null):
        """Underlay displayable that dispatches spatial focus and
        replays clicks on rollforward.
        """

        def event(self, ev: Any, x: int, y: int, st: float) -> Any:
            """Process directional focus events and ``rollforward``
            events.

            Args:
                ev (Any): The pygame input event.
                x (int): Mouse X coordinate.
                y (int): Mouse Y coordinate.
                st (float): Shown time in seconds.

            Returns:
                Any: The focus/rollforward result, or ``None`` if ``ev``
                    was not handled.
            """
            handled, focus_rv = handle_active_spatial_focus(ev)
            if handled:
                if focus_rv is not None:
                    return focus_rv
                raise IgnoreEvent()

            rf_target = getattr(renpy.game.interface, "roll_forward", None)
            if rf_target is None or not renpy.display.behavior.map_event(ev, "rollforward"):
                return None

            rb_id, node_name = get_current_rollback_info()
            actions = (
                RollForwardState.forward_clicks.get(node_name) if node_name is not None else None
            )
            if not actions:
                return None

            RollForwardState.rolled_forward_in_current_node = True
            rf_val = renpy.exports.roll_forward_core(rf_target)

            # Re-run all active sprite actions that were clicked during this statement.
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
        """Retrieve the ``(identifier, node_name)`` of the active
        rollback log entry.

        Returns:
            tuple[Any, Any]:
                ``(log.current.identifier, log.current.context.current)``,
                or ``(None, None)`` if rollback logging is unavailable.
        """
        log = getattr(renpy.game, "log", None)
        if log is None or log.current is None:
            return None, None
        return log.current.identifier, log.current.context.current


    def on_start_interact() -> None:
        """Reset per-node click tracking at the start of each new script
        interaction.

        Registered in ``config.start_interact_callbacks``. If the player
        advanced past the previous statement without clicking an active
        sprite or rolling forward (and the node is no longer on the
        rollforward stack), discards its recorded clicks.
        """
        if renpy.game.interface.trans_pause or not renpy.game.context().rollback:
            return

        rb_id, node_name = get_current_rollback_info()
        if rb_id is None or rb_id == RollForwardState.active_rb_id:
            return

        prev_node = RollForwardState.active_node_name
        if (
            prev_node is not None
            and not RollForwardState.clicked_in_current_node
            and not RollForwardState.rolled_forward_in_current_node
        ):
            forward_stack = getattr(renpy.game.log, "forward", ())
            if all(f.name != prev_node for f in forward_stack):
                RollForwardState.forward_clicks.pop(prev_node, None)

        RollForwardState.active_rb_id = rb_id
        RollForwardState.active_node_name = node_name
        RollForwardState.clicked_in_current_node = False
        RollForwardState.rolled_forward_in_current_node = False
        RollForwardState.current_node_clicks = python_list()


    def wrap_click(action: Any) -> Callable[[], Any] | None:
        """Wrap an ``ActiveImage`` button action for
        rollback/rollforward and interaction return.

        - Records executed actions in ``RollForwardState`` so rolling back and forward
            replays the sprite click.
        - Resets ``old_max_default = 0`` when an action fires so default focus calculation
            on the next interaction is not suppressed.
        - Stores non-jump return values in ``store._return`` and returns ``True`` during
            standard ``say``/`pause` interactions to end the current interaction cleanly.

        Args:
            action (Any): The Ren'Py action (or list of actions) to
                wrap, or ``None``.

        Returns:
            Callable[[], Any] | None: A zero-argument callable wrapping
                ``action``, or ``None`` if ``action`` is ``None``.
        """
        if action is None:
            return None

        def wrapped() -> Any:
            rb_id, node_name = get_current_rollback_info()
            if node_name is not None and renpy.game.context().rollback:
                RollForwardState.clicked_in_current_node = True
                RollForwardState.current_node_clicks.append(action)
                RollForwardState.forward_clicks[node_name] = python_list(
                    RollForwardState.current_node_clicks
                )

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
