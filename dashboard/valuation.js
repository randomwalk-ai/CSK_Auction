// Render valuation card (used in valuation tab)
function renderValuationCard(valuation) {
    const formColor = valuation.form_score >= 70 ? 'text-green-400' : 
                      valuation.form_score >= 50 ? 'text-yellow-400' : 'text-red-400';
    
    const verdictColor = valuation.auction_verdict === 'Must Buy' ? 'bg-green-600' :
                         valuation.auction_verdict === 'Strong Target' ? 'bg-blue-600' :
                         valuation.auction_verdict === 'Value Pick' ? 'bg-yellow-600' : 'bg-gray-600';
    
    return `
        <div class="bg-gray-800 rounded-xl overflow-hidden">
            <!-- Header -->
            <div class="bg-gradient-to-r from-gray-700 to-gray-800 p-6">
                <div class="flex justify-between items-start">
                    <div>
                        <h2 class="text-2xl font-bold text-yellow-400">${valuation.player_name}</h2>
                        <p class="text-gray-400">${valuation.role} | Age ${valuation.age} | ${valuation.country}</p>
                    </div>
                    <div class="text-right">
                        <div class="text-sm text-gray-400">Fair Market Value</div>
                        <div class="text-3xl font-bold text-green-400">₹${valuation.median_value} Cr</div>
                        <div class="text-xs text-gray-400">Range: ₹${valuation.floor} - ₹${valuation.ceiling} Cr</div>
                    </div>
                </div>
            </div>
            
            <!-- Metrics Grid -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 p-6 border-b border-gray-700">
                <div class="text-center">
                    <div class="text-sm text-gray-400">Form Score</div>
                    <div class="text-2xl font-bold ${formColor}">${valuation.form_score}</div>
                    <div class="text-xs">${valuation.form_trend}</div>
                </div>
                <div class="text-center">
                    <div class="text-sm text-gray-400">CSK Fit</div>
                    <div class="text-2xl font-bold text-purple-400">${valuation.csk_fit_score}</div>
                    <div class="text-xs">/100</div>
                </div>
                <div class="text-center">
                    <div class="text-sm text-gray-400">Confidence</div>
                    <div class="text-2xl font-bold text-blue-400">${valuation.confidence}%</div>
                    <div class="text-xs">${valuation.risk_category}</div>
                </div>
                <div class="text-center">
                    <div class="text-sm text-gray-400">Verdict</div>
                    <div class="text-lg font-bold px-3 py-1 rounded-full ${verdictColor} inline-block">${valuation.auction_verdict}</div>
                </div>
            </div>
            
            <!-- Phase Stats -->
            <div class="p-6 border-b border-gray-700">
                <h3 class="font-bold mb-4">📊 Phase-Wise Performance</h3>
                <div class="grid grid-cols-3 gap-4">
                    <div class="text-center">
                        <div class="text-xs text-gray-400">Powerplay</div>
                        <div class="text-sm">Bat SR: ${valuation.phase_stats?.powerplay?.bat_sr || 'N/A'}</div>
                        <div class="text-sm">Bowl Econ: ${valuation.phase_stats?.powerplay?.bowl_econ || 'N/A'}</div>
                    </div>
                    <div class="text-center">
                        <div class="text-xs text-gray-400">Middle</div>
                        <div class="text-sm">Bat SR: ${valuation.phase_stats?.middle?.bat_sr || 'N/A'}</div>
                        <div class="text-sm">Bowl Econ: ${valuation.phase_stats?.middle?.bowl_econ || 'N/A'}</div>
                    </div>
                    <div class="text-center">
                        <div class="text-xs text-gray-400">Death</div>
                        <div class="text-sm">Bat SR: ${valuation.phase_stats?.death?.bat_sr || 'N/A'}</div>
                        <div class="text-sm">Bowl Econ: ${valuation.phase_stats?.death?.bowl_econ || 'N/A'}</div>
                    </div>
                </div>
            </div>
            
            <!-- CSK Fit Reasons -->
            <div class="p-6 border-b border-gray-700">
                <h3 class="font-bold mb-3">✅ Why CSK Should Buy</h3>
                <ul class="list-disc list-inside text-sm text-gray-300 space-y-1">
                    ${valuation.csk_fit_reasons?.map(r => `<li>${r}</li>`).join('') || '<li>No specific reasons available</li>'}
                </ul>
            </div>
            
            <!-- Auction Strategy -->
            <div class="p-6">
                <h3 class="font-bold mb-3">🎯 Auction Strategy</h3>
                <div class="bg-gray-700 rounded-lg p-4">
                    <p class="text-gray-300">${valuation.auction_strategy}</p>
                    <div class="grid grid-cols-2 gap-4 mt-4 pt-4 border-t border-gray-600">
                        <div>
                            <div class="text-xs text-gray-400">Recommended Entry Bid</div>
                            <div class="text-lg font-bold text-green-400">₹${valuation.entry_bid} Cr</div>
                        </div>
                        <div>
                            <div class="text-xs text-gray-400">Walk Away Price</div>
                            <div class="text-lg font-bold text-red-400">₹${valuation.walk_away_price} Cr</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// Render comparison result
function renderComparison(comparison) {
    return `
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-gray-800 rounded-xl p-5">
                <h3 class="text-xl font-bold text-yellow-400 mb-4">${comparison.player1.name}</h3>
                <div class="space-y-3">
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Role:</span>
                        <span class="font-semibold">${comparison.player1.role}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Age:</span>
                        <span>${comparison.player1.age}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Form Score:</span>
                        <span class="font-bold ${comparison.player1.form >= 70 ? 'text-green-400' : comparison.player1.form >= 50 ? 'text-yellow-400' : 'text-red-400'}">${comparison.player1.form}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">CSK Fit:</span>
                        <span class="font-bold text-purple-400">${comparison.player1.csk_fit}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Death SR:</span>
                        <span>${comparison.player1.death_sr || 'N/A'}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Death Economy:</span>
                        <span>${comparison.player1.death_econ || 'N/A'}</span>
                    </div>
                    <div class="flex justify-between py-2">
                        <span class="text-gray-400">Est. Value:</span>
                        <span class="font-bold text-green-400">₹${comparison.player1.value} Cr</span>
                    </div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-5">
                <h3 class="text-xl font-bold text-yellow-400 mb-4">${comparison.player2.name}</h3>
                <div class="space-y-3">
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Role:</span>
                        <span class="font-semibold">${comparison.player2.role}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Age:</span>
                        <span>${comparison.player2.age}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Form Score:</span>
                        <span class="font-bold ${comparison.player2.form >= 70 ? 'text-green-400' : comparison.player2.form >= 50 ? 'text-yellow-400' : 'text-red-400'}">${comparison.player2.form}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">CSK Fit:</span>
                        <span class="font-bold text-purple-400">${comparison.player2.csk_fit}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Death SR:</span>
                        <span>${comparison.player2.death_sr || 'N/A'}</span>
                    </div>
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-gray-400">Death Economy:</span>
                        <span>${comparison.player2.death_econ || 'N/A'}</span>
                    </div>
                    <div class="flex justify-between py-2">
                        <span class="text-gray-400">Est. Value:</span>
                        <span class="font-bold text-green-400">₹${comparison.player2.value} Cr</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}