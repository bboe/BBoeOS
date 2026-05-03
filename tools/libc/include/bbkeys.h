#ifndef BBOEOS_LIBC_BBKEYS_H
#define BBOEOS_LIBC_BBKEYS_H
/* BBoeOS keyboard event codes for CONSOLE_IOCTL_TRY_GET_EVENT.
 *
 * Wire format (returned in EAX from the ioctl):
 *
 *     0                                    -> empty queue (no event)
 *     (pressed << 16) | code               -> one (key down or up) event
 *
 *     code     = BBKEY_* below (16-bit, > 0 for real keys)
 *     pressed  = 1 for press, 0 for release
 *
 * Codes are positional — BBKEY_W is "the W slot on a US keyboard"
 * regardless of the user's layout.  This mirrors Linux's evdev
 * KEY_* / SDL_Scancode model: the kernel keeps the layout-agnostic
 * code stable, and any layout (US, AZERTY, Dvorak) is layered on
 * top by the consumer.  ASCII translation still lives in the cooked
 * read(0, ...) path — that's a separate channel from this one.
 *
 * Modifier keys (Ctrl, Shift, Alt) are first-class first-class keys
 * here, with both press and release events delivered.  Cooked
 * input (read syscall) still folds them into Ctrl+letter etc. as
 * usual; only TRY_GET_EVENT consumers see standalone modifier
 * events.
 *
 * Producer: drivers/ps2.c ps2_handle_scancode -> ps2_broadcast_event
 * Consumer: fs/fd/console.c CONSOLE_IOCTL_TRY_GET_EVENT
 *
 * IMPORTANT: keep these values in sync with the matching block at
 * the top of src/drivers/ps2.c.  cc.py's preprocessor doesn't share
 * an include path with the userspace clang build so the header
 * can't be a single source of truth.
 */

#define BBKEY_RESERVED      0   /* sentinel — never emitted */

/* Modifier keys */
#define BBKEY_LSHIFT        1
#define BBKEY_RSHIFT        2
#define BBKEY_LCTRL         3
#define BBKEY_RCTRL         4
#define BBKEY_LALT          5
#define BBKEY_RALT          6
#define BBKEY_CAPSLOCK      7

/* Arrow keys */
#define BBKEY_UP            8
#define BBKEY_DOWN          9
#define BBKEY_LEFT          10
#define BBKEY_RIGHT         11

/* Action keys */
#define BBKEY_ESC           12
#define BBKEY_BACKSPACE     13
#define BBKEY_TAB           14
#define BBKEY_ENTER         15
#define BBKEY_SPACE         16

/* Letters: A=17 .. Z=42 */
#define BBKEY_A             17
#define BBKEY_B             18
#define BBKEY_C             19
#define BBKEY_D             20
#define BBKEY_E             21
#define BBKEY_F             22
#define BBKEY_G             23
#define BBKEY_H             24
#define BBKEY_I             25
#define BBKEY_J             26
#define BBKEY_K             27
#define BBKEY_L             28
#define BBKEY_M             29
#define BBKEY_N             30
#define BBKEY_O             31
#define BBKEY_P             32
#define BBKEY_Q             33
#define BBKEY_R             34
#define BBKEY_S             35
#define BBKEY_T             36
#define BBKEY_U             37
#define BBKEY_V             38
#define BBKEY_W             39
#define BBKEY_X             40
#define BBKEY_Y             41
#define BBKEY_Z             42

/* Digits: 0=43 .. 9=52 */
#define BBKEY_0             43
#define BBKEY_1             44
#define BBKEY_2             45
#define BBKEY_3             46
#define BBKEY_4             47
#define BBKEY_5             48
#define BBKEY_6             49
#define BBKEY_7             50
#define BBKEY_8             51
#define BBKEY_9             52

/* Punctuation that the current US-QWERTY keymap exposes. */
#define BBKEY_GRAVE         53  /* ` */
#define BBKEY_MINUS         54  /* - */
#define BBKEY_EQUALS        55  /* = */
#define BBKEY_LBRACKET      56  /* [ */
#define BBKEY_RBRACKET      57  /* ] */
#define BBKEY_BACKSLASH     58  /* \ */
#define BBKEY_SEMICOLON     59  /* ; */
#define BBKEY_APOSTROPHE    60  /* ' */
#define BBKEY_COMMA         61  /* , */
#define BBKEY_PERIOD        62  /* . */
#define BBKEY_SLASH         63  /* / */
#define BBKEY_KP_STAR       64  /* numpad * (Set-1 0x37) */

#endif
