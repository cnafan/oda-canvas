import axios from 'axios'

/** The backend's message for a failed request, which is safe to show. */
export function errorDetail(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') return detail[0].msg
  }
  return error instanceof Error ? error.message : 'The request failed.'
}
