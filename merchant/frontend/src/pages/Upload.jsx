import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

function Upload() {
  const [file, setFile] = useState(null)
  const [errors, setErrors] = useState([])
  const [progress, setProgress] = useState(0)
  const [status, setStatus] = useState('idle') // idle | uploading | done
  const navigate = useNavigate()

  const handleFileChange = (e) => {
    setErrors([])
    setStatus('idle')
    setProgress(0)
    const selected = e.target.files[0]
    if (!selected) return

    const validExtensions = ['.csv', '.xlsx', '.xls']
    const isValid = validExtensions.some((ext) => selected.name.toLowerCase().endsWith(ext))

    if (!isValid) {
      setErrors(['Please upload a .csv, .xlsx, or .xls file.'])
      return
    }
    setFile(selected)
  }

  const handleUpload = () => {
    if (!file) {
      setErrors(['Please select a file first.'])
      return
    }

    setErrors([])
    setStatus('uploading')
    setProgress(0)

    const merchantId = localStorage.getItem('merchantId')
    const formData = new FormData()
    formData.append('file', file)
    formData.append('merchantId', merchantId)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', import.meta.env.VITE_UPLOAD_API_URL)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        setProgress(Math.round((event.loaded / event.total) * 100))
      }
    }

    xhr.onload = () => {
      let response
      try {
        response = JSON.parse(xhr.responseText)
      } catch {
        response = null
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        setStatus('done')
        setTimeout(() => navigate('/products'), 1000)
      } else {
        const backendErrors = response?.errors || [response?.message || 'Upload failed. Please try again.']
        setErrors(backendErrors)
        setStatus('idle')
      }
    }

    xhr.onerror = () => {
      setErrors(['Network error — could not reach the upload server.'])
      setStatus('idle')
    }

    xhr.send(formData)
  }

  return (
    <div className="max-w-md mx-auto mt-16 p-8">
      <h1 className="text-2xl font-bold mb-6">Upload your product catalog</h1>

      <input
        type="file"
        accept=".csv,.xlsx,.xls"
        onChange={handleFileChange}
        className="mb-4 block w-full text-sm"
      />

      {file && <p className="text-sm text-gray-600 mb-4">Selected: {file.name}</p>}

      {errors.length > 0 && (
        <ul className="text-red-500 text-sm mb-4 list-disc pl-5">
          {errors.map((err, i) => (
            <li key={i}>{err}</li>
          ))}
        </ul>
      )}

      {status === 'uploading' && (
        <div className="mb-4">
          <div className="w-full bg-gray-200 rounded h-2">
            <div
              className="bg-black h-2 rounded transition-all"
              style={{ width: `${progress}%` }}
            />
          </div>
          <p className="text-sm text-gray-600 mt-1">{progress}%</p>
        </div>
      )}
      {status === 'done' && <p className="text-sm text-green-600 mb-4">Upload complete!</p>}

      <button
        onClick={handleUpload}
        disabled={status === 'uploading'}
        className="bg-black text-white rounded py-2 px-4 disabled:opacity-50"
      >
        Upload
      </button>
    </div>
  )
}

export default Upload