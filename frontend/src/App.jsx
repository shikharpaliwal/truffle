import { Route, Routes } from 'react-router-dom'
import Footer from './components/Footer'
import CoinPage from './pages/CoinPage'
import TablePage from './pages/TablePage'

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/" element={<TablePage />} />
        <Route path="/coin/:id" element={<CoinPage />} />
        <Route path="*" element={<TablePage />} />
      </Routes>
      <Footer />
    </>
  )
}
