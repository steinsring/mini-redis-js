import { VNodeType } from './vnode.js';
function addChange(changes, change, nextPath, defaultPath = change.path) {
    if (nextPath.length !== defaultPath.length || nextPath.some((index, depth) => index !== defaultPath[depth])) {
        change.nextPath = nextPath;
    }
    changes.push(change);
}
function diffProps(previous, next, path, nextPath, changes) {
    const set = Object.fromEntries(Object.entries(next)
        .filter(([name, value]) => !Object.hasOwn(previous, name) || previous[name] !== value));
    const remove = Object.keys(previous).filter(name => !Object.hasOwn(next, name));
    if (Object.keys(set).length || remove.length) {
        addChange(changes, { type: 'UPDATE_PROPS', path, set, remove }, nextPath);
    }
}
function keyOf(node) {
    return node.type === VNodeType.ELEMENT ? node.props['data-key'] : undefined;
}
function validateKeys(node) {
    if (node.type !== VNodeType.ELEMENT)
        return;
    const keys = new Set();
    for (const child of node.children) {
        const key = keyOf(child);
        if (key !== undefined) {
            if (keys.has(key))
                throw new TypeError('같은 부모 아래 중복 key: ' + key);
            keys.add(key);
        }
        validateKeys(child);
    }
}
function diffChildren(previous, next, parentPath, nextParentPath, changes, matchKeys) {
    const shared = Math.min(previous.length, next.length);
    for (let index = 0; index < shared; index++) {
        compare(previous[index], next[index], [...parentPath, index], [...nextParentPath, index], changes, matchKeys);
    }
    for (let index = previous.length - 1; index >= shared; index--) {
        addChange(changes, { type: 'REMOVE', path: [...parentPath, index] }, nextParentPath, parentPath);
    }
    for (let index = shared; index < next.length; index++) {
        addChange(changes, { type: 'INSERT', path: parentPath, index, node: next[index] }, [...nextParentPath, index], [...parentPath, index]);
    }
}
function diffKeyedChildren(previous, next, parentPath, nextParentPath, changes) {
    const keyed = new Map();
    const unkeyed = [];
    previous.forEach((child, index) => {
        const key = keyOf(child);
        if (key === undefined)
            unkeyed.push(index);
        else
            keyed.set(key, index);
    });
    let nextUnkeyed = 0;
    const matches = next.map(child => {
        const key = keyOf(child);
        return key === undefined ? unkeyed[nextUnkeyed++] : keyed.get(key);
    });
    const retained = new Set(matches);
    const order = previous.map((_, index) => index).filter(index => retained.has(index));
    for (let index = previous.length - 1; index >= 0; index--) {
        if (!retained.has(index)) {
            addChange(changes, { type: 'REMOVE', path: [...parentPath, index] }, nextParentPath, parentPath);
        }
    }
    // Apply structural changes left to right after removing unmatched children.
    matches.forEach((previousIndex, index) => {
        const nextPath = [...nextParentPath, index];
        const localPath = [...parentPath, index];
        if (previousIndex === undefined) {
            addChange(changes, { type: 'INSERT', path: parentPath, index, node: next[index] }, nextPath, localPath);
            order.splice(index, 0, -1);
        }
        else if (order[index] !== previousIndex) {
            addChange(changes, { type: 'MOVE', path: [...parentPath, previousIndex], index }, nextPath, localPath);
            order.splice(order.indexOf(previousIndex, index), 1);
            order.splice(index, 0, previousIndex);
        }
    });
    matches.forEach((previousIndex, index) => {
        if (previousIndex !== undefined) {
            compare(previous[previousIndex], next[index], [...parentPath, previousIndex], [...nextParentPath, index], changes, true);
        }
    });
}
function compare(previous, next, path, nextPath, changes, matchKeys) {
    if (previous.type === VNodeType.TEXT && next.type === VNodeType.TEXT) {
        if (previous.value !== next.value) {
            addChange(changes, { type: 'UPDATE_TEXT', path, value: next.value }, nextPath);
        }
        return;
    }
    if (previous.type !== VNodeType.ELEMENT || next.type !== VNodeType.ELEMENT || previous.tagName !== next.tagName) {
        addChange(changes, { type: 'REPLACE', path, node: next }, nextPath);
        return;
    }
    diffProps(previous.props, next.props, path, nextPath, changes);
    if (matchKeys && (previous.children.some(child => keyOf(child) !== undefined) || next.children.some(child => keyOf(child) !== undefined))) {
        diffKeyedChildren(previous.children, next.children, path, nextPath, changes);
    }
    else if (!matchKeys && (next.tagName === 'ul' || next.tagName === 'ol')) {
        const childChanges = [];
        diffChildren(previous.children, next.children, path, nextPath, childChanges, matchKeys);
        if (childChanges.length) {
            diffChildren(previous.children, [], path, nextPath, changes, matchKeys);
            diffChildren([], next.children, path, nextPath, changes, matchKeys);
        }
    }
    else {
        diffChildren(previous.children, next.children, path, nextPath, changes, matchKeys);
    }
}
export function diff(previous, next, { matchKeys = false } = {}) {
    if (matchKeys) {
        if (previous)
            validateKeys(previous);
        if (next)
            validateKeys(next);
    }
    if (previous == null) {
        return next == null ? [] : [{ type: 'INSERT', path: [], index: 0, node: next }];
    }
    if (next == null)
        return [{ type: 'REMOVE', path: [0] }];
    const changes = [];
    compare(previous, next, [0], [0], changes, matchKeys);
    return changes;
}
