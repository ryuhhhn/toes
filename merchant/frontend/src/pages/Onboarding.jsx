import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { supabase } from '../lib/supabaseClient'

function Onboarding() {
  const [merchantName, setMerchantName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')

    if (!merchantName.trim()) {
      setError('Please enter a merchant name.')
      return
    }

    setLoading(true)
    const { data, error: dbError } = await supabase
      .from('merchants')
      .insert([{ name: merchantName }]) // category gets added later, after CSV upload
      .select()
      .single()
    setLoading(false)

    if (dbError) {
      setError(dbError.message)
      return
    }

    localStorage.setItem('merchantId', data.id)
    navigate('/upload')
  }

  return (
    <div className="max-w-md mx-auto mt-16 p-8">
      <h1 className="text-2xl font-bold mb-6">Set up your store</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">Merchant name</label>
          <input
            type="text"
            value={merchantName}
            onChange={(e) => setMerchantName(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. Sunny's Boutique"
          />
        </div>

        {error && <p className="text-red-500 text-sm">{error}</p>}

        <button
          type="submit"
          disabled={loading}
          className="bg-black text-white rounded py-2 mt-2 disabled:opacity-50"
        >
          {loading ? 'Creating...' : 'Continue'}
        </button>
      </form>
    </div>
  )
}

export default Onboarding