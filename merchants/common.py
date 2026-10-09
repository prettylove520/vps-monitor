def one(root,selector):
    nodes=root.select(selector)
    if len(nodes)!=1:
        raise ValueError('匹配元素不是唯一一个：'+selector)
    return nodes[0]
