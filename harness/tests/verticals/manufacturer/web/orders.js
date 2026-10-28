/* A vertical tab reads its authenticated API through the common shell lifecycle. */
window.RocotoWorkspaceTabs.register('manufacturer', {
  orders: {
    async mount(root, context) {
      const response = await fetch('/api/manufacturer/orders', {signal: context.signal});
      if (!response.ok) throw new Error('Open the Orders tab with current workspace access.');
      const result = await response.json();
      if (!context.isCurrent()) return;
      const heading = document.createElement('h2');
      heading.textContent = 'Orders';
      const list = document.createElement('ul');
      for (const order of result.orders) {
        const item = document.createElement('li');
        item.textContent = `${order.id}: ${order.status}`;
        list.append(item);
      }
      root.replaceChildren(heading, list);
      return () => root.replaceChildren();
    }
  }
});
