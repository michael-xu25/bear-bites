import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            MenuBrowsingView()
                .tabItem {
                    Label("Menu", systemImage: "fork.knife")
                }

            FavoritesView()
                .tabItem {
                    Label("Favorites", systemImage: "heart.fill")
                }

            AddFavoriteView()
                .tabItem {
                    Label("Add Favorite", systemImage: "plus.circle")
                }
        }
    }
}

#Preview {
    ContentView()
}
