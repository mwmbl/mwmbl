import {Chart} from "chart.js/auto";

(async () => {
  Chart.defaults.font.size = 16;

  function createChart(elementId, labels, label) {
    const canvas = document.getElementById(elementId);
    return new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: label,
          borderWidth: 1
        }]
      },
      options: {
        scales: {
          y: {
            beginAtZero: true
          }
        },
        maintainAspectRatio: false
      }
    });
  }

  const resultsIndexedDailyChart = createChart('results-indexed-daily', null, "Results indexed by day");
  const urlsCrawledDailyChart = createChart('urls-by-day', null, "URLs crawled by day");
  const urlsCrawledHourlyChart = createChart('urls-by-hour', [...Array(24).keys()], "URLs crawled today by hour");
  const usersCrawledDailyChart = createChart('users-by-day', null, "Number of users crawling by day");
  const numUrlsInIndexDailyChart = createChart('num-index-urls-by-day', null, "Number of URLs in index by day");
  const numResultsInIndexDailyChart = createChart('num-index-results-by-day', null, "Number of results in index by day");

  const urlsByUserCanvas = document.getElementById('top-user-results');
  const byUserChart = new Chart(urlsByUserCanvas, {
    type: 'bar',
    data: {
      datasets: [{
        label: "Top users",
        borderWidth: 1
        // barThickness: 15
      }]
    },
    options: {
      scales: {
        x: {
          beginAtZero: true
        }
      },
      indexAxis: 'y',
      maintainAspectRatio: false
    }
  });

  const urlsByDomainCanvas = document.getElementById('urls-by-domain');
  const byDomainChart = new Chart(urlsByDomainCanvas, {
    type: 'bar',
    data: {
      datasets: [{
        label: "Top domains",
        borderWidth: 1
      }]
    },
    options: {
      scales: {
        x: {
          beginAtZero: true
        }
      },
      indexAxis: 'y',
      maintainAspectRatio: false
    }
  });

function numberWithCommas(x) {
  // From https://stackoverflow.com/a/2901298/660902
  return x.toString().replace(/\B(?<!\.\d*)(?=(\d{3})+(?!\d))/g, ",");
}

  function updateStats() {
    fetch("https://api.mwmbl.org/crawler/stats").then(result => {
      result.json().then(stats => {
        console.log("Stats", stats);

        const urlCountSpan = document.getElementById("num-urls");
        urlCountSpan.innerText = numberWithCommas(stats.urls_crawled_today);

        const numUsers = Object.values(stats.users_crawled_daily)[Object.keys(stats.users_crawled_daily).length - 1];
        const userCountSpan = document.getElementById("num-users");
        userCountSpan.innerText = numberWithCommas(numUsers);

        const numUrlsInIndex = Object.values(stats.urls_in_index_daily)[Object.keys(stats.urls_in_index_daily).length - 1];
        const numUrlsInIndexSpan = document.getElementById("num-index-urls");
        numUrlsInIndexSpan.innerText = numberWithCommas(numUrlsInIndex);

        const numResultsInIndex = Object.values(stats.results_in_index_daily)[Object.keys(stats.results_in_index_daily).length - 1];
        const numResultsInIndexSpan = document.getElementById("num-index-results");
        numResultsInIndexSpan.innerText = numberWithCommas(numResultsInIndex);

        resultsIndexedDailyChart.data.labels = Object.keys(stats.results_indexed_daily);
        resultsIndexedDailyChart.data.datasets[0].data = Object.values(stats.results_indexed_daily);
        resultsIndexedDailyChart.update();

        usersCrawledDailyChart.data.labels = Object.keys(stats.users_crawled_daily);
        usersCrawledDailyChart.data.datasets[0].data = Object.values(stats.users_crawled_daily);
        usersCrawledDailyChart.update();

        urlsCrawledHourlyChart.data.datasets[0].data = stats.urls_crawled_hourly;
        urlsCrawledHourlyChart.update();

        urlsCrawledDailyChart.data.labels = Object.keys(stats.urls_crawled_daily);
        urlsCrawledDailyChart.data.datasets[0].data = Object.values(stats.urls_crawled_daily);
        urlsCrawledDailyChart.update();

        console.log("Top users", stats.top_user_results);
        byUserChart.data.labels = stats.top_user_results.map(u => u[0]);
        byUserChart.data.datasets[0].data = stats.top_user_results.map(u => u[1]);
        byUserChart.update();

        byDomainChart.data.labels = stats.top_domains.map(d => d[0]);
        byDomainChart.data.datasets[0].data = stats.top_domains.map(d => d[1]);
        byDomainChart.update();

        numUrlsInIndexDailyChart.data.labels = Object.keys(stats.urls_in_index_daily);
        numUrlsInIndexDailyChart.data.datasets[0].data = Object.values(stats.urls_in_index_daily);
        numUrlsInIndexDailyChart.update();

        numResultsInIndexDailyChart.data.labels = Object.keys(stats.results_in_index_daily);
        numResultsInIndexDailyChart.data.datasets[0].data = Object.values(stats.results_in_index_daily);
        numResultsInIndexDailyChart.update();
      })
    });
  }

  updateStats();
  setInterval(() => {
    updateStats();
  }, 5000);

})();
