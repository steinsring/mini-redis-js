import { isRenderableNode, toDOM, VNodeType } from '../phase1/vnode.js';
import { diff } from '../phase1/diff.js';
import { patch } from '../phase1/patch.js';
const clickHandlers = new WeakMap();
let rendering = null;
function assertComponent(component) {
    if (typeof component !== 'function' || !/^[A-Z]/.test(component.name)) {
        throw new TypeError('컴포넌트는 대문자로 시작하는 함수여야 합니다.');
    }
}
function assertVNode(input) {
    const node = input;
    if (node?.type === VNodeType.TEXT && typeof node.value === 'string')
        return;
    if (node?.type === VNodeType.ELEMENT && typeof node.tagName === 'string' &&
        node.props && Array.isArray(node.children))
        return;
    throw new TypeError('컴포넌트는 VNode 하나를 반환해야 합니다.');
}
function collectChildren(input, children = []) {
    for (const value of input) {
        if (Array.isArray(value))
            collectChildren(value, children);
        else if (value == null || typeof value === 'boolean')
            continue;
        else if (typeof value === 'string' || typeof value === 'number') {
            children.push({ type: VNodeType.TEXT, value: String(value) });
        }
        else {
            assertVNode(value);
            children.push(value);
        }
    }
    return children;
}
function assertUniqueKeys(children) {
    const keys = new Set();
    for (const child of children) {
        const key = child.type === VNodeType.ELEMENT ? child.props['data-key'] : undefined;
        if (key === undefined)
            continue;
        if (keys.has(key))
            throw new TypeError('같은 부모 아래 중복 key: ' + key);
        keys.add(key);
    }
}
function renderChild(component, props) {
    assertComponent(component);
    const stack = rendering?.components;
    const record = { name: component.name, props, children: [] };
    stack?.at(-1)?.children.push(record);
    stack?.push(record);
    try {
        const node = component(props);
        assertVNode(node);
        return node;
    }
    finally {
        stack?.pop();
    }
}
export function h(tag, props, ...children) {
    const { key, ...attributes } = (props ?? {});
    if (key != null && typeof key !== 'string' && typeof key !== 'number') {
        throw new TypeError('key는 문자열이나 숫자여야 합니다.');
    }
    const descendants = collectChildren(children);
    if (typeof tag === 'function') {
        const node = renderChild(tag, (children.length ? { ...attributes, children: descendants } : attributes));
        if (key == null)
            return node;
        if (node.type !== VNodeType.ELEMENT)
            throw new TypeError('key는 요소에 지정할 수 있습니다.');
        const keyed = { ...node, props: { ...node.props, 'data-key': String(key) } };
        const handler = clickHandlers.get(node);
        if (handler)
            clickHandlers.set(keyed, handler);
        return keyed;
    }
    if (typeof tag !== 'string' || !/^[a-z][\w-]*$/.test(tag)) {
        throw new TypeError('h에 소문자로 시작하는 HTML 태그나 컴포넌트 함수를 전달하세요.');
    }
    assertUniqueKeys(descendants);
    const entries = [];
    let handler;
    for (const [name, value] of Object.entries(attributes)) {
        if (value == null)
            continue;
        if (name === 'onClick') {
            if (typeof value !== 'function')
                throw new TypeError('onClick에는 함수를 전달하세요.');
            handler = value;
            continue;
        }
        if (/^on/i.test(name) || !['string', 'number', 'boolean'].includes(typeof value)) {
            throw new TypeError('지원하지 않는 HTML 속성: ' + name);
        }
        const attribute = name === 'className' ? 'class' : name === 'htmlFor' ? 'for' : name.toLowerCase();
        if (typeof value === 'boolean' && !/^(aria|data)-/.test(attribute)) {
            if (value)
                entries.push([attribute, '']);
        }
        else {
            entries.push([attribute, String(value)]);
        }
    }
    const node = { type: VNodeType.ELEMENT, tagName: tag.toLowerCase(), props: Object.fromEntries(entries), children: descendants };
    if (key != null)
        node.props['data-key'] = String(key);
    if (handler)
        clickHandlers.set(node, handler);
    return node;
}
function rootRender() {
    if (!rendering || rendering.calculating || rendering.components.length !== 1) {
        throw new Error('Hook은 루트 컴포넌트가 렌더링되는 동안에만 호출할 수 있습니다.');
    }
    return rendering;
}
function nextSlot(hook) {
    const frame = rootRender();
    const index = frame.slots.length;
    const previous = frame.previous[index];
    if (frame.updating && previous?.hook !== hook) {
        throw new Error('렌더링마다 같은 순서로 Hook을 호출하세요. 위치: ' + index);
    }
    return { frame, index, previous: previous };
}
function dependencies(deps) {
    if (deps === undefined)
        return undefined;
    if (!Array.isArray(deps))
        throw new TypeError('Hook 의존성은 배열이어야 합니다.');
    return deps.slice();
}
function sameDependencies(previous, next) {
    return previous !== undefined && next !== undefined &&
        previous.length === next.length && previous.every((value, i) => Object.is(value, next[i]));
}
export function defineHook(hook) {
    if (typeof hook !== 'function' || !hook.name.startsWith('use')) {
        throw new TypeError('사용자 Hook의 이름은 use로 시작해야 합니다.');
    }
    return (...args) => {
        rootRender().log.push({ hook: hook.name, action: 'custom' });
        return hook(...args);
    };
}
export function useState(initialValue) {
    const { frame, index, previous } = nextSlot('useState');
    const { setState } = frame;
    const slot = previous ?? {
        hook: 'useState',
        value: initialValue,
        set(action) { setState(index, slot.set, action); },
    };
    frame.slots.push(slot);
    frame.log.push({ hook: slot.hook, index, action: previous ? 'read' : 'init' });
    return [slot.value, slot.set];
}
export function useMemo(calculate, deps) {
    const { frame, index, previous } = nextSlot('useMemo');
    const values = dependencies(deps);
    if (previous && sameDependencies(previous.deps, values)) {
        frame.slots.push(previous);
        frame.log.push({ hook: previous.hook, index, action: 'reuse' });
        return previous.value;
    }
    frame.calculating = true;
    try {
        const value = calculate();
        frame.slots.push({ hook: 'useMemo', deps: values, value });
        frame.log.push({ hook: 'useMemo', index, action: 'calculate' });
        return value;
    }
    finally {
        frame.calculating = false;
    }
}
export function useEffect(effect, deps) {
    const { frame, index, previous } = nextSlot('useEffect');
    if (typeof effect !== 'function')
        throw new TypeError('useEffect에는 함수를 전달하세요.');
    const values = dependencies(deps);
    const slot = { hook: 'useEffect', deps: values, cleanup: previous?.cleanup };
    frame.slots.push(slot);
    if (!previous || !sameDependencies(previous.deps, values)) {
        frame.effects.push({ slot, effect, index, previousDeps: previous?.deps });
    }
    else {
        frame.log.push({ hook: slot.hook, index, action: 'keep', deps: values });
    }
}
function mapClicks(vnode, node, mapped) {
    if (vnode.type === VNodeType.TEXT)
        return;
    const handler = clickHandlers.get(vnode);
    if (handler)
        mapped.set(node, handler);
    const children = Array.from(node.childNodes).filter(isRenderableNode);
    vnode.children.forEach((child, index) => mapClicks(child, children[index], mapped));
}
export class FunctionComponent {
    #clicks = new WeakMap();
    #pending = [];
    #committing = false;
    #flushing = false;
    constructor(component, container, { props = {}, onRender } = {}) {
        assertComponent(component);
        this.component = component;
        this.container = container;
        this.props = props;
        this.onRender = onRender;
        this.hooks = [];
        this.history = [];
        this.currentTree = null;
    }
    #dispatch = (event) => {
        const route = [];
        for (let node = event.target; node && node !== this.container; node = node.parentNode) {
            const handler = this.#clicks.get(node);
            if (handler)
                route.push([node, handler]);
        }
        for (const [node, handler] of route) {
            handler.call(node, event);
            if (event.cancelBubble)
                break;
        }
    };
    #render() {
        if (rendering || this.#committing)
            throw new Error('컴포넌트 렌더링을 중첩할 수 없습니다.');
        const componentTree = { name: this.component.name, props: this.props, children: [] };
        const frame = {
            previous: this.hooks, updating: this.currentTree !== null,
            slots: [], effects: [], log: [], components: [componentTree], componentTree,
            setState: (index, setter, action) => this.#setState(index, setter, action),
        };
        rendering = frame;
        try {
            const { component, props } = this;
            const tree = component(props);
            assertVNode(tree);
            if (frame.updating && frame.slots.length !== frame.previous.length) {
                throw new Error('렌더링마다 같은 개수의 Hook을 호출하세요.');
            }
            return { ...frame, tree };
        }
        finally {
            rendering = null;
        }
    }
    #setState(index, setter, action) {
        if (rendering)
            throw new Error('렌더링 중에는 상태를 변경할 수 없습니다.');
        if (!this.currentTree)
            return;
        this.#pending.push({ index, setter, action });
        this.#flush();
    }
    #flush() {
        if (this.#committing || this.#flushing)
            return;
        this.#flushing = true;
        try {
            while (this.#pending.length) {
                const { index, setter, action } = this.#pending.shift();
                const slot = this.hooks[index];
                if (slot?.hook !== 'useState' || slot.set !== setter)
                    continue;
                const previous = slot.value;
                const next = typeof action === 'function' ? action(previous) : action;
                if (Object.is(previous, next))
                    continue;
                const before = this.hooks;
                slot.value = next;
                try {
                    this.update();
                }
                catch (error) {
                    if (this.hooks === before)
                        slot.value = previous;
                    throw error;
                }
            }
        }
        catch (error) {
            this.#pending.length = 0;
            throw error;
        }
        finally {
            this.#flushing = false;
        }
    }
    #save(tree) {
        this.currentTree = tree;
        this.history.push(tree);
        if (this.history.length > 10)
            this.history.shift();
    }
    #complete(frame, phase, previous, changes) {
        this.hooks = frame.slots;
        const mapped = new WeakMap();
        mapClicks(frame.tree, this.container.firstChild, mapped);
        this.#clicks = mapped;
        this.#committing = true;
        try {
            for (const { slot, effect, index, previousDeps } of frame.effects) {
                const cleanup = slot.cleanup;
                slot.cleanup = undefined;
                if (cleanup) {
                    frame.log.push({ hook: slot.hook, index, action: 'cleanup', deps: previousDeps });
                    cleanup();
                }
                frame.log.push({ hook: slot.hook, index, action: 'run', deps: slot.deps });
                const returned = effect();
                if (typeof returned === 'function')
                    slot.cleanup = returned;
            }
            this.onRender?.({ phase, previous, next: frame.tree, changes, componentTree: frame.componentTree, log: frame.log });
        }
        catch (error) {
            this.#pending.length = 0;
            throw error;
        }
        finally {
            this.#committing = false;
        }
        this.#flush();
    }
    mount() {
        if (this.currentTree)
            throw new Error('이미 마운트된 컴포넌트입니다.');
        const frame = this.#render();
        const changes = diff(null, frame.tree, { matchKeys: true });
        this.container.replaceChildren(toDOM(frame.tree, this.container.ownerDocument));
        this.container.addEventListener('click', this.#dispatch);
        this.#save(frame.tree);
        this.#complete(frame, 'mount', null, changes);
    }
    update() {
        if (!this.currentTree)
            throw new Error('먼저 컴포넌트를 마운트하세요.');
        const previous = this.currentTree;
        const frame = this.#render();
        const changes = diff(previous, frame.tree, { matchKeys: true });
        if (changes.length) {
            patch(this.container, changes);
            this.#save(frame.tree);
        }
        this.#complete(frame, 'update', previous, changes);
        return changes;
    }
    unmount() {
        if (rendering || this.#committing)
            throw new Error('렌더링 또는 Effect 처리 중에는 제거할 수 없습니다.');
        if (!this.currentTree)
            return;
        const previous = this.currentTree;
        const hooks = this.hooks;
        const log = [];
        const errors = [];
        this.container.removeEventListener('click', this.#dispatch);
        this.container.replaceChildren();
        this.#clicks = new WeakMap();
        this.currentTree = null;
        this.hooks = [];
        this.history.length = 0;
        this.#pending.length = 0;
        this.#committing = true;
        try {
            for (const [index, slot] of hooks.entries()) {
                if (slot.hook !== 'useEffect' || !slot.cleanup)
                    continue;
                log.push({ hook: slot.hook, index, action: 'cleanup', deps: slot.deps });
                try {
                    slot.cleanup();
                }
                catch (error) {
                    errors.push(error);
                }
            }
            this.onRender?.({
                phase: 'unmount', previous, next: null, componentTree: null, log,
                changes: [{ type: 'REMOVE', path: [0] }],
            });
        }
        catch (error) {
            errors.push(error);
        }
        finally {
            this.#committing = false;
        }
        if (errors.length === 1)
            throw errors[0];
        if (errors.length > 1)
            throw new AggregateError(errors, '컴포넌트 정리 중 여러 오류가 발생했습니다.');
    }
}
