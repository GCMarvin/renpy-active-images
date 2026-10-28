# Active Images for Ren'Py

In Ren'Py, making a character sprite clickable usually means moving it into a screen as an `imagebutton`, which loses `show` attribute tracking, `zorder` layering, and ATL transforms.

`active_images.rpy` adds three statements (`activate`, `deactivate`, and `call active`) that turn regular images on screen into interactive buttons. Active images respond to clicks, hovers, and keyboard or controller focus while still working like normal `show` images.

## Installation

Copy [`active_images.rpy`](active_images.rpy) into your project's `game/libs/` folder. Make sure `game/libs/libs.txt` exists (an empty file is fine) so Ren'Py loads `game/libs/` before your script files.

## Usage

### 1. Basic Selection

`activate` turns an image into a clickable button, `call active` waits for one to be clicked, and `deactivate` turns it back into a normal image:

```renpy
label start:
    show eileen at left
    show lucy at right

    activate eileen action Return("eileen")
    activate lucy action Return("lucy")

    call active

    deactivate eileen
    deactivate lucy

    if _return == "eileen":
        e "You picked me!"
    else:
        l "You picked me!"
```

> [!TIP]
> You can add a dialogue block to `call active` to keep a prompt on screen while waiting for input:
>
> ```renpy
> call active:
>     e "Who do you want to talk to?"
> ```

### 2. Showing and Activating in One Step

`activate` accepts the same arguments as `show` (`attributes`, `at`, `zorder`, `behind`, `onlayer`, `as`, `with`), so you can show and activate an image in a single statement:

```renpy
label start:
    activate eileen at left action Return("eileen")
    activate lucy at right action Return("lucy")

    call active
```

> [!TIP]
> Use `alternate` to handle right-clicks or secondary actions:
>
> ```renpy
> activate eileen action Return("eileen") alternate Notify("Clicked!")
> ```

Calling `activate` on an image that is already active updates only the properties you specify and keeps the rest:

```renpy
activate lucy sensitive False
```

### 3. Using `show` on Active Images

Once an image is active, normal `show` statements update its expression or position without turning off its button behavior. Use `activate` and `deactivate` only to control interactivity, and use `show` for everything else:

```renpy
label start:
    activate eileen at left action Return("eileen")
    activate lucy at right action Return("lucy")

    show eileen happy
    show lucy mad

    call active
```

### 4. Timed `call active`

`call active` accepts an optional timeout in seconds. If time runs out before an active image is clicked, `_return` is `None`:

```renpy
label start:
    activate eileen action Return(True)

    call active 3.0:
        "Click Eileen within 3 seconds!"

    deactivate eileen

    if _return:
        e "Just in time!"
    else:
        e "Too slow!"
```

> [!NOTE]
> `call active` counts as a choice point, so text skipping automatically stops here just like it does at a `menu`.

### 5. Clicking During Dialogue

Active images do not require `call active` to work. They remain clickable during regular dialogue lines:

```renpy
label start:
    show eileen at left
    activate lucy at right action Jump("talk_to_lucy")

    e "You can click on Lucy at any time while I'm talking."
    e "If you don't click her, I'll just keep going."

    deactivate lucy
    return

label talk_to_lucy:
    deactivate lucy
    l "Thanks for listening to me instead!"
    return
```

### 6. Hover Effects

Active images trigger `on hover:` and `on idle:` blocks in their transforms:

```renpy
transform highlight:
    on idle:
        ease 0.2 matrixcolor BrightnessMatrix(0.0)
    on hover:
        ease 0.2 matrixcolor BrightnessMatrix(0.1)

label start:
    activate eileen at left, highlight action Return("eileen")
    activate lucy at right, highlight action Return("lucy")

    call active
```

> [!NOTE]
> Active images integrate with Ren'Py's directional focus system automatically. Players using arrow keys or controller D-pads can navigate between sprites on screen and UI buttons.

### 7. Moving Images

Both `activate` and `deactivate` accept ATL blocks, just like `show`. Hitboxes follow moving images automatically:

```renpy
label start:
    activate eileen action Return():
        xalign 0.0
        block:
            ease 1.0 xalign 1.0
            ease 1.0 xalign 0.0
            repeat

    call active:
        "Click Eileen while she's running!"

    deactivate eileen:
        ease 0.5 xalign 0.5

    e "You caught me!"
```

### 8. Layering and Occlusion

Any part of an active image covered by another image or UI screen in front of it is ignored by the mouse:

```renpy
label start:
    show bush zorder 1

    activate lucy zorder 0 action Return():
        xoffset 100

    call active:
        "Find Lucy."

    deactivate lucy zorder 2:
        ease 0.5 xoffset 0
    l "You found me!"
```

> [!NOTE]
> `focus_mask` defaults to `True` on active images (unlike standard Ren'Py buttons), so transparent areas and pixels behind foreground objects are ignored automatically.

## Reference

### `activate`

```renpy
activate <image_spec> [action <expr>] [hovered <expr>] [unhovered <expr>] [alternate <expr>] [sensitive <expr>] [selected <expr>] [focus_mask <expr>] [with <transition>]:
    # Optional ATL statements
    ...
```

| Clause | Default | Description |
| :--- | :--- | :--- |
| `action` | `NullAction()` | Action run when the image is clicked. |
| `hovered` | `None` | Action run when the image is hovered or focused. |
| `unhovered` | `None` | Action run when the image loses hover or focus. |
| `alternate` | `None` | Action run on right-click or long-press. |
| `sensitive` | `True` | Whether the image responds to input. |
| `selected` | `False` | Whether the image is in a selected state. |
| `focus_mask` | `True` | Whether only non-transparent, unblocked pixels are clickable. |

### `deactivate`

Turns an active image back into a normal image, returns its hover transforms to `idle`, and applies any optional `show` arguments or ATL block.

```renpy
deactivate <image_spec> [with <transition>]:
    # Optional ATL statements
    ...
```

### `call active`

Waits until an active image is clicked (or the optional timeout in seconds expires).

```renpy
call active
call active 5.0
call active:
    e "Pick someone."
```

## License

[MIT License](LICENSE.md)
