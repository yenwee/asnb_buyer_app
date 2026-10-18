"""Dismiss login announcements through their controls, then verify hit targets."""

import time

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException


_POPUP_STATE = """
const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
        s.visibility !== 'hidden' && Number(s.opacity) !== 0;
};
const layers = [...document.querySelectorAll(
    'div.fixed.inset-0, [role="dialog"][aria-modal="true"]'
)].filter(visible);
const controls = layers.flatMap(layer => [...layer.querySelectorAll(
    'svg.absolute.cursor-pointer, button[aria-label="Close"], ' +
    'button[aria-label="Tutup"], [data-dismiss="modal"]'
)]).filter(visible).filter(e => {
    const r = e.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
    return top && (e === top || e.contains(top));
});
const target = arguments[0] || document.querySelector('input[name="username"]');
let reachable = false;
if (target && visible(target) && !target.disabled) {
    if (!layers.length) target.scrollIntoView({block: 'center', behavior: 'instant'});
    const r = target.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
    reachable = !!top && (target === top || target.contains(top));
}
return {blocked: layers.length > 0, controls, reachable};
"""


def dismiss_login_popups(driver, target=None, appearance_wait=5, timeout=15):
    """Handle delayed/stacked popups; raise if an overlay remains or target is blocked.

    Class tokens deliberately avoid dependence on Tailwind class order or z-index.
    Re-query after each click because React can replace the modal DOM.
    """
    started = time.monotonic()
    deadline = started + timeout
    clear_since = None
    while time.monotonic() < deadline:
        try:
            state = driver.execute_script(_POPUP_STATE, target)
            if state['blocked'] or not state['reachable']:
                clear_since = None
                if state['controls']:
                    driver.execute_script("""
                        arguments[0].dispatchEvent(new MouseEvent('click', {
                            view: window, bubbles: true, cancelable: true
                        }));
                    """, state['controls'][0])
            else:
                clear_since = clear_since or time.monotonic()
                if (time.monotonic() - started >= appearance_wait and
                        time.monotonic() - clear_since >= 0.5):
                    return
        except StaleElementReferenceException:
            # A replaced explicit target must be reacquired by the caller.
            if target is not None:
                raise
            clear_since = None
        time.sleep(0.25)
    raise TimeoutException('Login popup did not close or login control remains obstructed')
