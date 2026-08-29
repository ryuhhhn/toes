import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

let supabase

if (!supabaseUrl || !supabaseAnonKey) {
  console.warn('Supabase env vars are missing — check your .env file. Database calls will fail until this is set.')
  supabase = null
} else {
  supabase = createClient(supabaseUrl, supabaseAnonKey)
}

export { supabase }