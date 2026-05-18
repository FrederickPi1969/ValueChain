export function modalityClass(modality) {
  if (modality === 'current_fact') return 'current';
  if (modality === 'risk_hypothetical') return 'risk';
  if (modality === 'forward_looking') return 'forward';
  if (modality === 'strategic') return 'strategic';
  return '';
}

export function truncate(value, max = 220) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

export function shortRelation(value) {
  return String(value || '').replaceAll('_dependency', '').replaceAll('_', ' ');
}
