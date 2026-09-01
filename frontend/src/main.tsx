import React from 'react'
import ReactDOM from 'react-dom/client'
import '@fontsource-variable/manrope/wght.css'
import App from './App'
import { initMonitoring } from './services/monitoring'
import './styles/global.css'
import './styles/marketing-tokens.css'

initMonitoring()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><App /></React.StrictMode>
)
