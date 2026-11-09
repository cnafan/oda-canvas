/** Friendly label for the existing OpenAN registry component. */
export function componentDisplayName(component: { name: string; namespace: string }): string {
  return component.namespace === 'openan' && component.name === 'registry-center'
    ? 'openan-registry-center'
    : component.name
}
